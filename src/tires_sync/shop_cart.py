"""The client's cart and orders (the owner 2026-10-30): a buyer puts positions of the own catalog in the cart, then places an order. The
order lands on «Передано с точки» (from_point) with the manager of the clients' orders as responsible (a setting of the owner, never a
login in the code) and the source «Каталог клиента»; the positions carry the client's price. The cart keeps only what and how many: the
price and the stock are taken anew at the checkout (the panel passes the positions the client may buy now, routes_shop._visible).
Everything here is of one login, the one signed in; a client never reaches another's cart, contacts or orders."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import connections, economics, orders, store, terms as terms_store, users
from .panel import economics_view

RESPONSIBLE_KEY = "shop_orders_responsible"
MAX_LINES = 50
MAX_QTY = 100                                                 # a position of an order holds 100 at most (orders._qty)
ADDRESS_MIN, ADDRESS_MAX = 5, 300
# what a client reads of the stage of the own order: never the inside of the work (the owner 2026-10-30)
STATUS = {"from_point": "Принят", "new": "Принят", "talk": "Принят", "wait": "Принят", "placed": "Оформлен", "moving": "В пути",
          "at_point": "Готов к выдаче", "done": "Выполнен", "cancel": "Отменён", "oth": "Отменён", "return_given": "Отменён", "return": "Отменён"}
STATUS_OTHER = "В работе"                                     # a stage the owner made (no code)


class CartError(ValueError):
    """What the client did cannot be done; the text is for the client."""


@dataclass(frozen=True)
class Line:
    """A position of the cart as the client may buy it now: `item` is client_catalog.Visible (None: not sold to the client any more)."""
    goods_id: str
    qty: int
    item: object


# ---- the manager of the clients' orders --------------------------------------------------------------------------------------------

def responsible_setting(conn) -> str:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (RESPONSIBLE_KEY,)).fetchone()
    return row[0] if row else ""


def responsible(conn) -> str:
    """Who answers for an order of a client: the owner's choice while that login is an active staff user; else the general default; else
    the owner (never the client who made the card: an empty responsible would read as its author)."""
    from . import order_notify
    who = responsible_setting(conn)
    if who and users.exists(conn, who) and users.is_active(conn, who) and users.get_role(conn, who)["role"] not in ("client", "point"):
        return who
    return orders.default_responsible(conn) or order_notify.get_owner_login(conn) or ""


def set_responsible(conn, login: str, now: str, author: str) -> None:
    """The owner's choice; "" = back to the general default. Commits."""
    login = users.normalize(login) if login else ""
    if login and (not users.exists(conn, login) or users.get_role(conn, login)["role"] in ("client", "point")):
        raise CartError("Выберите сотрудника панели.")
    conn.execute("INSERT INTO app_state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (RESPONSIBLE_KEY, login))
    store.write_audit(conn, now, author, "shop_responsible", {"login": login})
    conn.commit()


# ---- the cart -----------------------------------------------------------------------------------------------------------------------

def _qty(value) -> int:
    try:
        qty = int(str(value).strip())
    except ValueError:
        raise CartError("Количество — целое число.")
    if not 0 <= qty <= MAX_QTY:
        raise CartError("Количество от 1 до %d шт." % MAX_QTY)
    return qty


def cart(conn, login: str) -> Dict[str, int]:
    """goods_id → qty of the login's cart, in the order put."""
    return {gid: qty for gid, qty in conn.execute("SELECT goods_id, qty FROM shop_cart WHERE login = ? ORDER BY added_at, goods_id", (login,))}


def put(conn, login: str, item, qty, now: str, add: bool = False) -> int:
    """Set (or with `add`, add to) the quantity of a position the client may buy (`item`: client_catalog.Visible, the panel checked it);
    0 takes it out. Not more than the stock. Returns the quantity in the cart. Commits."""
    if not login:
        raise CartError("Войдите снова.")
    qty = _qty(qty)
    have = cart(conn, login)
    gid = item.offer.goods_id
    if add:
        if qty < 1:
            raise CartError("Количество от 1 до %d шт." % MAX_QTY)
        qty += have.get(gid, 0)
    qty = min(qty, int(item.offer.stock), MAX_QTY)
    if qty <= 0:
        conn.execute("DELETE FROM shop_cart WHERE login = ? AND goods_id = ?", (login, gid))
    else:
        if gid not in have and len(have) >= MAX_LINES:
            raise CartError("В корзине уже %d позиций: оформите заказ или уберите лишнее." % MAX_LINES)
        conn.execute("INSERT INTO shop_cart (login, goods_id, qty, added_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (login, goods_id) DO UPDATE SET qty = excluded.qty", (login, gid, qty, now))
    conn.commit()
    return max(qty, 0)


def drop(conn, login: str, goods_id: str) -> None:
    conn.execute("DELETE FROM shop_cart WHERE login = ? AND goods_id = ?", (login, goods_id))
    conn.commit()


def lines(conn, login: str, visible_of) -> List[Line]:
    """The cart with what the client may buy now: `visible_of(goods_ids)` → {goods_id: Visible} (the panel's _visible)."""
    have = cart(conn, login)
    found = visible_of(list(have)) if have else {}
    return [Line(gid, qty, found.get(gid)) for gid, qty in have.items()]


def total(cart_lines: List[Line]) -> int:
    return sum(l.qty * l.item.price for l in cart_lines if l.item is not None)


# ---- the client's contacts ---------------------------------------------------------------------------------------------------------

def profile(conn, login: str) -> dict:
    row = conn.execute("SELECT phone, address FROM client_profile WHERE login = ?", (login,)).fetchone()
    return {"phone": row[0], "address": row[1]} if row else {"phone": "", "address": ""}


def save_profile(conn, login: str, phone, address, now: str) -> dict:
    """«Профиль» of the client (the owner 2026-10-30): the phone and the address of delivery, checked as at the checkout; the next
    checkout offers them."""
    contacts = clean_contacts(phone, address)
    conn.execute("INSERT INTO client_profile (login, phone, address, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT (login) DO UPDATE SET "
                 "phone = excluded.phone, address = excluded.address, updated_at = excluded.updated_at",
                 (login, contacts["phone"], contacts["address"], now))
    conn.commit()
    return contacts


def clean_contacts(phone, address) -> dict:
    """The phone and the address of a checkout: both are needed (the owner 2026-10-30); CartError with the field otherwise."""
    tel = connections.normalize_phone(phone)
    if not tel:
        raise CartError("Телефон: российский номер, например +7 900 000-00-00.")
    text = " ".join(str(address or "").split())
    if not ADDRESS_MIN <= len(text) <= ADDRESS_MAX:
        raise CartError("Адрес: от %d до %d знаков." % (ADDRESS_MIN, ADDRESS_MAX))
    return {"phone": tel, "address": text}


# ---- the order ---------------------------------------------------------------------------------------------------------------------

def _client_name(item) -> str:
    o = item.offer
    model = " ".join((o.model_raw or "").split())
    return " ".join(("%s %s %d/%d R%d %s%s" % (item.brand_label, model, o.width, o.height, o.diameter, o.load or "", o.speed or "")).split())[:200]


def _item_args(conn, item, qty: int, day: str) -> dict:
    """orders.add_item for a position of the cart: the client's price; the supplier's price and terms as for any order (the unit economy);
    no fee of Avito (not sold through Avito)."""
    o = item.offer
    season = economics_view.season_of(o)
    return dict(goods_id=o.goods_id, code=o.code, name=_client_name(item), brand_key=o.brand_key, season=season, diameter=o.diameter,
                territory=o.territory_rn, qty=qty, sale_unit=item.price, supplier_price=float(o.base_price),
                terms=economics.pick_terms(terms_store.list_terms(conn), o.brand_key, season, o.diameter, day), fee_pct=0, promo_pct=0)


def place(conn, login: str, name: str, cart_lines: List[Line], contacts: dict, point: Optional[dict], comment: str, now: str,
          expected_total: Optional[int] = None) -> int:
    """The order of the cart: all or nothing. The cart that was read must still be the cart (a second press, another tab: «корзина пуста»);
    a position not sold any more or above the stock refuses the whole order (the client corrects the cart). The contacts are kept for the
    next order. `expected_total`: the sum the client saw on the button; a price changed since refuses (the client looks again).
    Returns the order's id; the caller fires the rules and tells the manager."""
    if not cart_lines:
        raise CartError("Корзина пуста.")
    for l in cart_lines:
        if l.item is None:
            raise CartError("Некоторых шин в корзине уже нет в наличии: уберите их и оформите снова.")
        if l.qty > int(l.item.offer.stock):
            raise CartError("%s: в наличии %d шт., поправьте количество." % (_client_name(l.item), int(l.item.offer.stock)))
        if l.qty > MAX_QTY:
            raise CartError("%s: не больше %d шт. в одной строке заказа." % (_client_name(l.item), MAX_QTY))
    if expected_total is not None and expected_total != total(cart_lines):
        raise CartError("Цены изменились: было %s ₽, стало %s ₽. Проверьте корзину и оформите снова." % (_rub(expected_total), _rub(total(cart_lines))))
    stage = orders.ensure_from_point_stage(conn)              # it saves the stage it makes: before the order's own transaction
    comment = " ".join(str(comment or "").split())[:orders.MAX_COMMENT - ADDRESS_MAX - 40]
    text = ("Самовывоз: %s" % point["name"]) if point else ("Доставка: %s" % contacts["address"])
    fields = {"client_name": name or login, "client_phone": contacts["phone"], "point_id": str(point["id"]) if point else "",
              "source": "shop", "responsible": responsible(conn), "comment": "%s%s" % (text, (". " + comment) if comment else "")}
    try:
        conn.execute("UPDATE shop_cart SET qty = qty WHERE login = ?", (login,))           # a write first: the lock is ours from here
        if cart(conn, login) != {l.goods_id: l.qty for l in cart_lines}:                      # a second press, another tab: not this cart
            raise CartError("Корзина изменилась или уже оформлена: проверьте её и оформите снова." if cart(conn, login) else "Корзина пуста.")
        conn.execute("DELETE FROM shop_cart WHERE login = ?", (login,))
        card_id = orders.create_card(conn, fields, now, login, require_contact=False, commit=False, stage=stage, many_open=True)
        for l in cart_lines:
            orders.add_item(conn, card_id, now=now, author=login, commit=False, **_item_args(conn, l.item, l.qty, now[:10]))
        conn.execute("INSERT INTO shop_order (order_id, login, created_at) VALUES (?, ?, ?)", (card_id, login, now))
        conn.execute("INSERT INTO client_profile (login, phone, address, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT (login) DO UPDATE SET "
                     "phone = excluded.phone, address = excluded.address, updated_at = excluded.updated_at",
                     (login, contacts["phone"], contacts["address"], now))
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return card_id


def message(card: dict, name: str, total_sum: int, link: str) -> str:
    """The text for the manager: who, how much, where to open it (first: a long text is cut at its end), then what and the comment."""
    rows = ["%s × %d — %s ₽" % (i["name"], i["qty"], _rub(i["qty"] * i["sale_unit"])) for i in card["items"]]
    head = "Заказ №%d из каталога клиента: %s, %s. Сумма: %s ₽." % (card["id"], name, card["client_phone"], _rub(total_sum))
    return "\n".join([head] + ([link] if link else []) + rows + ([card["comment"]] if card["comment"] else []))


def _rub(value) -> str:
    return "{:,.0f}".format(float(value)).replace(",", " ")


# ---- «Мои заказы» ------------------------------------------------------------------------------------------------------------------

def _shown_name(item: dict) -> str:
    """The name of a position for the client: a position a manager added has the supplier's own name, its code is cut out (as the
    catalog does, client_catalog._name)."""
    name = item["name"]
    if item.get("code"):
        name = re.sub(re.escape(item["code"]), " ", name, flags=re.I)
    return " ".join(name.split())


def status(stage: dict) -> str:
    return STATUS.get(stage.get("code") or "", STATUS_OTHER)


def my_orders(conn, login: str) -> List[dict]:
    """The login's orders of the catalog, the newest first: number, date, status for the client, sum. Nothing of the inside."""
    out = []
    for order_id, created_at in conn.execute("SELECT order_id, created_at FROM shop_order WHERE login = ? ORDER BY order_id DESC", (login,)):
        view = my_order(conn, login, order_id)
        if view is not None:
            out.append(view)
    return out


def my_order(conn, login: str, order_id) -> Optional[dict]:
    """One order of the login (None: not the login's or not of the catalog): status, positions with the client's price, the sum."""
    if not re.fullmatch(r"\d{1,12}", str(order_id)):
        return None
    row = conn.execute("SELECT created_at FROM shop_order WHERE order_id = ? AND login = ?", (int(order_id), login)).fetchone()
    if row is None:
        return None
    try:
        card = orders.get_card(conn, int(order_id))
    except orders.OrderError:
        return None
    items = [{"name": _shown_name(i), "qty": i["qty"], "price": i["sale_unit"]} for i in orders.list_items(conn, card["id"])]
    return {"id": card["id"], "created_at": row[0], "status": status(card["stage"]), "items": items,
            "total": sum(i["qty"] * i["price"] for i in items)}
