"""The card funnel per position: views → contacts → sales → margin, and the first stage where it drops.
Thresholds are starting values (a rule of thumb, not a law): they are constants here so that they can be tuned in one place."""
from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Tuple

MIN_OBSERVED_DAYS = 7      # fewer days of observation: too early to judge
MIN_VIEWS = 20             # fewer views in the period: the problem is visibility, not the card
LOW_CONTACT_RATE = 0.02    # contacts per view below this: the card does not convince
MIN_CONTACTS = 3           # this many contacts and still no sale: the problem is after the contact

PACK_SUFFIX = re.compile(r"-x\d+$")

# order of the stages, most urgent first (used to sort the table)
CODES = ("few_views", "few_contacts", "no_sales", "ok", "selling", "watching")
LABELS = {
    "few_views": ("Мало просмотров", "Объявление почти не видят: проверьте заголовок, главное фото и цену рядом с соседями."),
    "few_contacts": ("Смотрят, но не пишут", "Просмотров достаточно, контактов мало: проверьте цену, фото и описание."),
    "no_sales": ("Пишут, но не покупают", "Контакты есть, продаж нет: проверьте цену после торга, остаток и срок."),
    "ok": ("В норме", "Воронка в порядке, ждём продаж."),
    "selling": ("Продаётся", "Есть продажи за период."),
    "watching": ("Набирается статистика", "Объявление наблюдаем меньше недели: выводов пока нет."),
}


def diagnose(views: int, contacts: int, pieces_sold: int, observed_days: int) -> Tuple[str, bool]:
    """(code, reliable). `reliable` is False when there is too little data to act on the verdict."""
    if observed_days < MIN_OBSERVED_DAYS:
        return "watching", False
    if pieces_sold > 0:
        return "selling", True
    if contacts >= MIN_CONTACTS:
        return "no_sales", True
    if views < MIN_VIEWS:
        return "few_views", False
    if contacts / views < LOW_CONTACT_RATE:
        return "few_contacts", True
    return "ok", True


def base_goods_id(ad_id: str) -> str:
    """The set of four («goods-x4») belongs to the same position as the single tyre."""
    return PACK_SUFFIX.sub("", (ad_id or "").split("_")[0])      # `<code>`, `<code>-x4`, `<code>_a4`, `<code>_<stamp>_r<date>`


def rows(conn, since: str, until: str, today: date) -> List[dict]:
    """One row per position (single ad and set of four together) for the days since..until."""
    positions: Dict[str, dict] = {}
    from . import mirror_feed
    for avito_id, account_id, ad_id, title, price, first_seen in conn.execute(
            "SELECT avito_id, account_id, ad_id, title, price, first_seen FROM avito_ad WHERE ad_id IS NOT NULL AND ad_id <> '' AND status = 'active'"):
        goods = mirror_feed.code_for_id(conn, account_id, ad_id) or base_goods_id(ad_id)
        pos = positions.setdefault(goods, {"goods_id": goods, "title": title, "price": price, "ads": [], "first_seen": first_seen or ""})
        pos["ads"].append((account_id, avito_id))
        if ad_id == goods:                       # the single tyre names the position and sets its price
            pos["title"], pos["price"] = title, price
        if first_seen and (not pos["first_seen"] or first_seen < pos["first_seen"]):
            pos["first_seen"] = first_seen
    stats: Dict[Tuple[int, int], Tuple[int, int, int]] = {}
    for account_id, avito_id, views, contacts, favorites in conn.execute(
            "SELECT account_id, avito_id, SUM(views), SUM(contacts), SUM(favorites) FROM ad_stat WHERE day >= ? AND day <= ? GROUP BY account_id, avito_id",
            (since, until)):
        stats[(account_id, avito_id)] = (views, contacts, favorites)
    sold: Dict[str, Tuple[int, float]] = {}
    for goods_id, pieces, margin in conn.execute(
            "SELECT goods_id, SUM(pieces), SUM(margin_total) FROM sale WHERE day >= ? AND day <= ? AND reversed_at IS NULL GROUP BY goods_id",
            (since, until)):
        sold[goods_id] = (pieces, margin)
    out = []
    for goods, pos in positions.items():
        views = contacts = favorites = 0
        for key in pos["ads"]:
            v, c, f = stats.get(key, (0, 0, 0))
            views, contacts, favorites = views + v, contacts + c, favorites + f
        pieces, margin = sold.get(goods, (0, 0.0))
        try:
            observed = (today - date.fromisoformat(pos["first_seen"][:10])).days + 1
        except ValueError:
            observed = 0
        code, reliable = diagnose(views, contacts, pieces, observed)
        out.append({"goods_id": goods, "title": pos["title"], "price": pos["price"], "views": views, "contacts": contacts,
                    "favorites": favorites, "rate": (contacts / views) if views else 0.0, "pieces": pieces, "margin": round(margin, 2),
                    "observed_days": observed, "code": code, "reliable": reliable, "label": LABELS[code][0], "advice": LABELS[code][1]})
    out.sort(key=lambda r: (CODES.index(r["code"]), -r["views"], r["title"].lower()))
    return out


def summarize(items: List[dict]) -> dict:
    views = sum(r["views"] for r in items)
    contacts = sum(r["contacts"] for r in items)
    counts = {code: 0 for code in CODES}
    for r in items:
        counts[r["code"]] += 1
    return {"positions": len(items), "views": views, "contacts": contacts, "favorites": sum(r["favorites"] for r in items),
            "rate": (contacts / views) if views else 0.0, "pieces": sum(r["pieces"] for r in items),
            "margin": round(sum(r["margin"] for r in items), 2), "counts": counts}


def last_collected(conn) -> str:
    row = conn.execute("SELECT MAX(seen_at) FROM avito_ad").fetchone()
    return row[0] or ""
