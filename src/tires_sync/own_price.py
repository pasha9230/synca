"""Own prices of positions below the purchase: the price stays as the owner set it (the owner 2026-10-29), the owner hears of it once.
An own price in percent that follows the purchase down without price_guard's question: the account's responsible hears of it once (the owner 2026-11-04)."""
from __future__ import annotations

import json
from typing import Dict, List, Tuple

from . import connections, store
from .pricing import own_price

MARK = "own-below:"          # app_state: "<MARK><account>:<goods_id>" -> the own price already told of
FALL_MARK = "own-fall:"      # app_state: "<FALL_MARK><account>:<goods_id>" -> JSON [the purchase our price is counted from, our price] last seen


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
    (its mark goes) or its own price changed; a position away from today's unload keeps its mark."""
    marks = {k[len(MARK):]: v for k, v in conn.execute("SELECT key, value FROM app_state WHERE key LIKE ?", (MARK + "%",))}
    lines = []
    keep = {k: v for k, v in marks.items() if k.partition(":")[2] not in offers}     # away from the unload for a while: told already
    for account, goods_id, price, base, value in below_cost(conn, settings, offers):
        key = "%d:%s" % (account["id"], goods_id)
        keep[key] = value
        if marks.get(key) != value:
            offer = offers[goods_id]
            lines.append("%s · %s (%s): своя цена %s, закупка %s" % (
                connections.display_name(conn, account["id"]), offer.name_orig or offer.name, offer.code, _rub(price), _rub(base)))
    return lines, keep


def remember(conn, keep: Dict[str, str]) -> None:
    conn.execute("DELETE FROM app_state WHERE key LIKE ?", (MARK + "%",))
    conn.executemany("INSERT INTO app_state (key, value) VALUES (?, ?)", [(MARK + k, v) for k, v in keep.items()])
    conn.commit()


def _in_feed(conn, account: dict, eff, offers) -> Dict[str, object]:
    """The positions whose ads this account holds on Avito now (read after the publication), with the price they stand at (None: a mirrored ad,
    whose price is always counted): the only ones where «наша цена» is a price somebody sees."""
    if not account["live"]:
        return {}
    if account["feed_mode"] == "mirror":
        from . import mirror_feed
        skip = dict(mirror_feed.unavailable(conn, account["id"], offers, eff.min_stock),
                    **mirror_feed.excluded_by_type(conn, account["id"], connections.get_excluded_types(conn, account["id"])))
        return {mirror_feed.supplier_code(i): None for i in mirror_feed.stored_ids(conn, account["id"]) if i not in skip}
    return {goods_id: item.price for goods_id, item in store.load_published(conn, account["id"]).items()}


def _fall_mark(value):
    """(purchase, our price) of a kept mark; None: unreadable, as if none."""
    try:
        was_base, was_price = json.loads(value)
        return float(was_base), float(was_price)
    except (TypeError, ValueError):
        return None


def falls(conn, settings, offers: Dict[str, object], account_ids) -> Tuple[List[dict], Dict[str, str]]:
    """The own prices in percent that went down after the purchase since the last look, and the marks to keep now. `account_ids`: the accounts
    whose feed this cycle built; another feed account keeps its marks and is told by a later cycle.
    The purchase here is what our price is counted from (price_guard.base_for): a fall held for the owner does not move it, so it is never told
    twice, and its end within 5 % is told as the fall it is. «Принять» is the owner's own step: not told, what follows is counted from the
    price accepted. The mark of a position told about keeps
    its old value until `told_falls`: an undelivered one is told by the next cycle, one held at the old price once it moves. A position away from today's unload keeps its mark;
    a first sight, a position off Avito, an own price no more in percent say nothing."""
    from . import price_guard
    marks = {k[len(FALL_MARK):]: v for k, v in conn.execute("SELECT key, value FROM app_state WHERE key LIKE ?", (FALL_MARK + "%",))}
    guard = price_guard.load(conn)
    wanted = set(account_ids)
    events, keep = [], {}
    feed_accounts = connections.list_feed_accounts(conn)
    known = {a["id"] for a in feed_accounts}
    for key, value in marks.items():
        account_id = key.partition(":")[0]
        if account_id.isdigit() and int(account_id) in known - wanted:
            keep[key] = value
    for account in feed_accounts:
        if account["id"] not in wanted:
            continue
        eff = connections.effective_settings(conn, account["id"], settings)
        in_feed = None
        for goods_id, own in sorted(eff.own_prices.items()):
            if own[0] != "pct":
                continue
            key = "%d:%s" % (account["id"], goods_id)
            offer = offers.get(goods_id)
            current = float(getattr(offer, "base_price", 0) or 0)
            if current <= 0:                                        # away from the unload or a bad price: nothing known today
                if key in marks:
                    keep[key] = marks[key]
                continue
            base, why = price_guard.base_for(guard, goods_id, current)
            price = own_price(base, own)
            if price is None:
                if key in marks:
                    keep[key] = marks[key]
                continue
            mark = json.dumps([base, price])
            was = _fall_mark(marks.get(key))
            keep[key] = mark
            row = guard["anchors"].get(goods_id)
            if was is not None and row is not None and row["how"] == "accepted" and was[0] == float(row["prev"] or 0):
                was = (float(row["base_price"]), own_price(float(row["base_price"]), own))
            if was is None or base >= was[0] or why:
                continue
            if in_feed is None:
                in_feed = _in_feed(conn, account, eff, offers)
            if goods_id not in in_feed:
                continue
            keep[key] = marks[key]
            if in_feed[goods_id] not in (None, price):              # the ad is held at its old price (low stock, a guard, own point): told when it moves
                continue
            events.append({"account_id": account["id"], "key": key, "mark": mark, "line": "%s · %s (%s): закупка %s → %s, наша цена %s → %s" % (
                connections.display_name(conn, account["id"]), offer.name_orig or offer.name, offer.code,
                _rub(was[0]), _rub(base), _rub(was[1]), _rub(price))})
    return events, keep


def falls_text(events: List[dict]) -> str:
    lines = [e["line"] for e in events]
    shown = "\n".join(lines[:20]) + ("\nи ещё %d" % (len(lines) - 20) if len(lines) > 20 else "")
    return ("Tires Sync: закупка снизилась, своя цена в процентах считается от новой закупки (%d). Цены за одну шину. Чтобы поменять — "
            "карточка позиции каталога, «Своя цена».\n%s" % (len(lines), shown))


def remember_falls(conn, keep: Dict[str, str]) -> None:
    conn.execute("DELETE FROM app_state WHERE key LIKE ?", (FALL_MARK + "%",))
    conn.executemany("INSERT INTO app_state (key, value) VALUES (?, ?)", [(FALL_MARK + k, v) for k, v in keep.items()])
    conn.commit()


def told_falls(conn, events: List[dict]) -> None:
    """These went through: not told again while the purchase stays."""
    conn.executemany("UPDATE app_state SET value = ? WHERE key = ?", [(e["mark"], FALL_MARK + e["key"]) for e in events])
    conn.commit()


def _rub(value) -> str:
    return "%s ₽" % "{:,}".format(int(round(value))).replace(",", " ")
