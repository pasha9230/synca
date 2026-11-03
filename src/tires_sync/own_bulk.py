"""«Ручные цены» of the catalog (the owner 2026-11-03): one change of the own prices of many positions, counted for each account apart.

From the purchase (the anchor of price_guard, or the last purchase the archive knows for a position the supplier took away) or from the
position's own price of that account now (applied or in the draft); in % or in ₽, up or down. A percent to the purchase stays a percent
({"pct": x}, as the own price of the card); every other result is a sale price ({"price": n}), up to tens as every own price. A position
without its own price in an account is skipped there when the base is the own price (never priced from the feed instead: the owner asked
to change the own prices, a brand's price is not one); one whose purchase is not known gets no percent and no «от закупки»."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from decimal import Decimal
from typing import Dict, Optional

from . import connections
from .config import OWN_PRICE_MAX, own_price_value
from .pricing import TENS, _up_to, own_price, position_price

BASES = ("purchase", "own")
UNITS = ("pct", "rub")
JUMP = 0.30                # as service.OWN_JUMP: a price this far from the price now is asked once more at «Применить»
SKIPS = {"no_own": "нет своей цены на этом аккаунте", "no_purchase": "закупка неизвестна",
         "out": "цена вышла бы за пределы 1…%d ₽" % OWN_PRICE_MAX}


def plan(prices: dict, purchases: Dict[str, float], accounts, op: Optional[dict], redo: bool = False) -> dict:
    """What one press writes. `prices`: price_overrides now (the draft on top), `purchases`: {goods_id: purchase, 0 unknown}, `op`: None (reset
    to the feed's settings) or {"base": BASES, "unit": UNITS, "delta": signed number}. With the purchase as the base a position that has its own
    price keeps it unless `redo`. {"set": {key: value | None}, "kept": [keys], "same": [keys], "skipped": {reason: [keys]}}."""
    out = {"set": {}, "kept": [], "same": [], "skipped": {}}
    for account_id in sorted({int(a) for a in accounts}):
        for goods_id in sorted(purchases):
            key = "%d:%s" % (account_id, goods_id)
            now = prices.get(key)
            if op is None:
                if now:
                    out["set"][key] = None
                continue
            value, why = _value(op, now, float(purchases[goods_id] or 0), redo)
            if why == "kept":
                out["kept"].append(key)
            elif why:
                out["skipped"].setdefault(why, []).append(key)
            elif value == now:
                out["same"].append(key)
            else:
                out["set"][key] = value
    return out


def _value(op: dict, now: Optional[dict], purchase: float, redo: bool):
    delta = Decimal(str(op["delta"]))
    if op["base"] == "purchase":
        if now and not redo:
            return None, "kept"
        if purchase <= 0:
            return None, "no_purchase"
        if op["unit"] == "pct":
            return {"pct": float(delta) + 0.0}, ""
        price = _up_to(Decimal(str(purchase)) + delta, TENS)
    else:
        if not now:
            return None, "no_own"
        current = own_price(purchase, own_price_value(now))
        if current is None:                                   # a percent of a purchase not known: no price to count from
            return None, "no_purchase"
        price = _up_to(Decimal(current) * (1 + delta / 100) if op["unit"] == "pct" else Decimal(current) + delta, TENS)
    if not 1 <= price <= OWN_PRICE_MAX:
        return None, "out"
    return {"price": price}, ""


def fingerprint(changes: dict) -> str:
    """The mark of what a press writes: «В черновик» writes only what «было → стало» showed."""
    return hashlib.sha1(json.dumps(changes, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def rows(conn, general, offers: dict, purchases: Dict[str, float], changes: dict, gone=(), pct_limit: Optional[float] = None) -> list:
    """«было → стало» of a press, a row per account × position: the price of one tyre now (the draft on top: `general`) and after, and why it is
    likely asked once more at «Применить» (below the supplier's price, more than JUMP away, a percent past `pct_limit`; «Применить» itself
    compares with the published price). The risky rows first."""
    accounts, out = {}, []
    names = {a["id"]: a["name"] for a in connections.list_accounts(conn)}
    for key, value in changes.items():
        account_id, _, goods_id = key.partition(":")
        account_id = int(account_id)
        if account_id not in accounts:
            accounts[account_id] = connections.effective_settings(conn, account_id, general)
        eff, base = accounts[account_id], float(purchases.get(goods_id) or 0)
        offer = dataclasses.replace(offers[goods_id], base_price=base)
        was = position_price(offer, eff)
        new = own_price(base, own_price_value(value)) if value else position_price(offer, dataclasses.replace(eff, own_prices={}))
        why, cost = [], float(offers[goods_id].base_price or 0)              # «ниже закупки» as «Применить» asks it: the supplier's price
        if value and new is not None and cost > 0 and new < cost:
            why.append("ниже закупки")
        if value and new and was and abs(new / was - 1) > JUMP:
            why.append("%+d %% к цене сейчас" % round((new / was - 1) * 100))
        if value and "pct" in value and pct_limit is not None and abs(value["pct"]) > pct_limit:
            why.append("процент больше %g" % pct_limit)
        out.append({"key": key, "goods_id": goods_id, "account": names.get(account_id, "аккаунт %d" % account_id),
                    "name": offers[goods_id].name_orig or offers[goods_id].name, "code": offers[goods_id].code, "gone": goods_id in gone,
                    "purchase": base or None, "was": was, "now": new, "value": value, "why": why})
    out.sort(key=lambda r: (not r["why"], r["name"], r["account"]))
    return out
