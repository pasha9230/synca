"""Own prices of positions below the purchase: the price stays as the owner set it (the owner 2026-10-29), the owner hears of it once."""
from __future__ import annotations

from typing import Dict, List, Tuple

from . import connections
from .pricing import own_price

MARK = "own-below:"          # app_state: "<MARK><account>:<goods_id>" -> the own price already told of


def below_cost(conn, settings, offers: Dict[str, object]) -> List[Tuple[dict, str, int, float, str]]:
    """(account, goods_id, price, purchase, mark value) of every fixed own price of a feed account below the purchase of today's unload.
    A percent below the purchase is the owner's own choice, not news: it is marked in the catalog only."""
    out = []
    for account in connections.list_feed_accounts(conn):
        eff = connections.effective_settings(conn, account["id"], settings)
        for goods_id, own in sorted(eff.own_prices.items()):
            offer = offers.get(goods_id)
            base = getattr(offer, "base_price", 0) or 0
            if own[0] != "price" or offer is None or base <= 0:
                continue
            price = own_price(base, own)
            if price is not None and price < base:
                out.append((account, goods_id, price, base, str(own[1])))
    return out


def fresh_events(conn, settings, offers) -> Tuple[List[str], Dict[str, str]]:
    """The lines not told yet and the marks to keep once they are: a position is told again only after it came back above the purchase
    (its mark goes) or its own price changed."""
    marks = {k[len(MARK):]: v for k, v in conn.execute("SELECT key, value FROM app_state WHERE key LIKE ?", (MARK + "%",))}
    lines, keep = [], {}
    for account, goods_id, price, base, value in below_cost(conn, settings, offers):
        key = "%d:%s" % (account["id"], goods_id)
        keep[key] = value
        if marks.get(key) != value:
            offer = offers[goods_id]
            lines.append("%s · %s (%s): своя цена %s, закупка %s" % (
                account["name"], offer.name_orig or offer.name, offer.code, _rub(price), _rub(base)))
    return lines, keep


def remember(conn, keep: Dict[str, str]) -> None:
    conn.execute("DELETE FROM app_state WHERE key LIKE ?", (MARK + "%",))
    conn.executemany("INSERT INTO app_state (key, value) VALUES (?, ?)", [(MARK + k, v) for k, v in keep.items()])
    conn.commit()


def _rub(value) -> str:
    return "%s ₽" % "{:,}".format(int(round(value))).replace(",", " ")
