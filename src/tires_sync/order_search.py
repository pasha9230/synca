"""The search of a position for an order by its name: «icon tires автограф» finds Ikon Tyres Autograph. Case, Russian or Latin letters, a usual
misspelling and the wrong keyboard layout do not matter. Only positions with a stock are offered, each with the price of its ad."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional

from . import catalog_archive, order_offers, orders, store, terms as terms_store
from .catalog import resolve_brand
from .description import BRAND_RU
from .panel import economics_view, service

LIMIT = 10
_TRANSLIT = dict(zip("абвгдеёзийклмнопрстуфхыэ", "abvgdeeziiklmnoprstufhye"),
                 ж="zh", ц="ts", ч="ch", ш="sh", щ="sch", ъ="", ь="", ю="yu", я="ya")
_EN, _RU = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`", "йцукенгшщзхъфывапролджэячсмитьбюё"
_TO_RU, _TO_EN = str.maketrans(_EN, _RU), str.maketrans(_RU, _EN)
_SOUNDS = (("dzh", "g"), ("dg", "g"), ("j", "g"), ("ph", "f"), ("ck", "k"), ("c", "k"), ("q", "k"), ("x", "ks"), ("w", "v"), ("y", "i"), ("v", "u"))


@lru_cache(maxsize=65536)
def sound(word: str) -> str:
    """How a word sounds, the same for its Russian and Latin spelling: Latin letters, close sounds made one, vowels and double letters dropped
    (Autograph and автограф are both «tgrf», Ikon, Icon and Айкон «kn», Tyres and Тайерс «trs»)."""
    text = "".join(_TRANSLIT.get(ch, ch) for ch in word.lower())
    for old, new in _SOUNDS:
        text = text.replace(old, new)
    text = re.sub(r"[^a-z]", "", text)
    text = re.sub(r"(.)\1+", r"\1", text)
    return re.sub(r"[aeiou]", "", text)


def _words(texts) -> List[str]:
    return [w for t in texts if t for w in re.split(r"[^0-9a-zа-яё]+", t.lower()) if w]


@lru_cache(maxsize=65536)
def _sounds(*texts) -> frozenset:
    return frozenset(sound(w) for w in _words(texts))


@lru_cache(maxsize=65536)
def _literal(width: int, height: int, diameter: int, *texts) -> tuple:
    """The fields of a position as the catalog search reads them (service.offer_matches), lowered and packed once."""
    size = ["%d/%d R%d" % (width, height, diameter), "%d/%d/%d" % (width, height, diameter), "%d %d %d" % (width, height, diameter),
            "%d%d%d" % (width, height, diameter)]
    fields = tuple(f.lower() for f in list(texts) + size if f)
    return fields, tuple(service._compact(f) for f in fields)


def _score(offer, avito_brand: str, words: List[str]) -> int:
    """0 when a word of the query is not found in the position; otherwise 3 for each word found as the catalog search finds it (same fields and rule), 2 by an equal sound,
    1 by the start of a sound (a word still being typed, at least three sounds)."""
    sounds = _sounds(offer.name_orig, offer.name, offer.brand_key, offer.brand_raw, avito_brand, BRAND_RU.get(avito_brand, ""), offer.model_raw)
    fields, packed = _literal(offer.width, offer.height, offer.diameter, offer.name_orig or "", offer.name or "", offer.brand_key or "",
                              avito_brand.lower(), offer.model_raw or "", offer.code or "", offer.goods_id or "")
    total = 0
    for word in words:
        cw = service._compact(word)
        if any(word in f for f in fields) or (len(cw) >= 2 and any(cw in p for p in packed)):
            total += 3
            continue
        key = sound(word)
        if len(key) >= 2 and key in sounds:
            total += 2
        elif len(key) >= 3 and any(s.startswith(key) for s in sounds):
            total += 1
        else:
            return 0
    return total


def _find(parsed, names: dict, query: str) -> list:
    words = query.lower().split()
    found = []
    for offer in parsed.offers.values():
        if offer.stock <= 0:
            continue
        score = _score(offer, names.get(offer.brand_key, ""), words)
        if score:
            found.append((score, score == 3 * len(words), offer))
    found.sort(key=lambda p: (-p[0], -p[2].stock, p[2].name_orig or p[2].name))
    return found


def _clean(query: str) -> str:
    return " ".join((query or "").replace("·", " ").split())[:80]


def scored(conn, parsed, ref, query: str) -> list:
    """[(score, exact, offer)] of the positions with a stock that the query finds, the best first; `exact` when every word is found as the catalog search
    finds it (no sound guess). A query typed in the wrong keyboard layout is tried switched when it finds nothing."""
    query = _clean(query)
    if not query:
        return []
    brand_map = store.load_brand_map(conn)
    names = {key: resolve_brand(key, ref, brand_map) or "" for key in {o.brand_key for o in parsed.offers.values()}}
    found = _find(parsed, names, query)
    for other in (query.lower().translate(_TO_RU), query.lower().translate(_TO_EN)):
        if found or other == query.lower():
            continue
        found = _find(parsed, names, other)
    return found


def find(conn, parsed, ref, query: str) -> list:
    """The positions with a stock that the query finds, the best first."""
    return [offer for _score_, _exact, offer in scored(conn, parsed, ref, query)]


def same_named(parsed, text: str) -> list:
    """The positions of the unload (with a stock or not) whose supplier's name is exactly `text`, case and extra spaces aside."""
    wanted = catalog_archive.same_name(text)
    if not wanted:
        return []
    return [o for o in parsed.offers.values() if wanted in (catalog_archive.same_name(o.name), catalog_archive.same_name(o.name_orig))]


def typed(conn, parsed, ref, text: str):
    """The position a manager typed and sent without choosing from the list: a code of the catalog or an article as it is, the only position whose
    supplier's name is exactly the text, or the only position whose every word is found exactly. Returns (offer or None, how many positions the
    text finds), so that a guess by sound is never added silently."""
    named = same_named(parsed, text)            # before the trim of the search: a full name of the supplier (the owner 2026-11-24)
    if len(named) == 1:
        return named[0], 1
    text = _clean(text)
    if text in parsed.offers:
        return parsed.offers[text], 1
    by_code = [o for o in parsed.offers.values() if text and o.code and o.code.lower() == text.lower()]
    if len(by_code) == 1:
        return by_code[0], 1
    if named:                                   # the same name twice in the unload: the manager chooses in the list
        return None, len(named)
    found = scored(conn, parsed, ref, text)
    exact = [offer for _score_, is_exact, offer in found if is_exact]
    return (exact[0] if len(exact) == 1 and len(found) == 1 else None), len(found)


SHOWN_VARIANTS = 3                              # a journal record names at most this many positions of the same name


class NotLocated(orders.OrderError):
    """locate found no position, or several: the text is the record of the journal (its first line the point, the search below)."""


@dataclass
class Located:
    """The position locate found: `how` is «id» (by its code in today's unload), «name» (by the supplier's exact name in today's unload) or
    «base» (only in the permanent base of positions: the unload has it no more); `info` what the base knows of it (catalog_archive.item_info)."""
    offer: object
    how: str
    info: Optional[dict] = None
    searched: str = ""

    def note(self) -> str:
        """The record of the journal that says where the position came from (no purchase price: the point reads it); "" for one found by its code."""
        if self.how == "id":
            return ""
        name = self.offer.name_orig or self.offer.name
        if self.how == "name":
            return "Позиция найдена по названию поставщика: «%s» (код %s), остаток сегодня %d шт.\nИскали: %s.\nВ выгрузке сегодня по коду: нет; по названию: да." % (
                name, self.offer.goods_id, self.offer.stock, self.searched)
        info = self.info or {}
        return ("Позиция взята из базы позиций: в выгрузке поставщика сегодня её нет.\n«%s» (код %s): у поставщика последний раз %s, остаток тогда %s шт.\n"
                "Проверьте наличие у поставщика перед заказом.\nИскали: %s." % (name, self.offer.goods_id, _day(info.get("last_seen")),
                                                                             info.get("last_stock", "?"), self.searched))

    def purchase_note(self) -> str:
        """The office's record of the purchase price a position of the base came with (the last one known); "" for one of today's unload."""
        if self.how != "base":
            return ""
        return "Цена закупки позиции «%s» из базы позиций: %s ₽ (последняя известная, %s)." % (
            self.offer.name_orig or self.offer.name, format(float(self.offer.base_price or 0), ".0f"), _day((self.info or {}).get("last_seen")))


def first_line(err) -> str:
    """The point of a NotLocated record for a message on the page: its first line, from a capital letter."""
    line = str(err).split("\n")[0].strip()
    return line[:1].upper() + line[1:]


def _day(stamp) -> str:
    return str(stamp or "")[:10] or "неизвестно когда"


def _searched(goods_id, names) -> str:
    parts = ["код %s" % goods_id] if goods_id else []
    parts += ["название «%s»" % n[:120] for n in names]
    return "; ".join(parts) or "ничего"


def _variants(offers_info) -> str:
    lines = ["— «%s» (код %s)%s" % (o.name_orig or o.name, o.goods_id, ", остаток %s шт." % s if s is not None else "")
             for o, s in offers_info[:SHOWN_VARIANTS]]
    more = len(offers_info) - SHOWN_VARIANTS
    return "\n".join(lines + (["и ещё %d" % more] if more > 0 else []))


def locate(conn, parsed, goods_id: str = "", names=()) -> Located:
    """The one position an order takes, from the supplier's data (the owner 2026-11-24: the ad of Avito only points at it), for a chat's ad and a row
    typed in the card alike: its code in today's unload; else the one position of the unload whose supplier's name is exactly one of `names`
    (the ad's title, the text typed); else the one position of the permanent base of positions (catalog_archive) with that code or that name.
    Never a guess by sound: several positions of one name are not taken. Raises NotLocated with the record of the journal."""
    goods_id = str(goods_id or "").strip()
    names = [n for n in (" ".join(str(x or "").split()) for x in names) if n]
    searched = _searched(goods_id, names)
    if goods_id and goods_id in parsed.offers:
        return Located(parsed.offers[goods_id], "id", searched=searched)
    for name in names:
        named = same_named(parsed, name)
        if len(named) == 1:
            return Located(named[0], "name", searched=searched)
        if named:
            raise NotLocated("по названию нашлось позиций: %d, ни одна не взята — выберите нужную в карточке заказа.\nИскали: %s.\n"
                             "В выгрузке сегодня: %d с таким названием.\n%s" % (len(named), searched, len(named),
                                                                                  _variants([(o, o.stock) for o in named])))
    if goods_id:
        offer, info = catalog_archive.last_offer(conn, goods_id), catalog_archive.item_info(conn, goods_id)
        if offer is not None:
            return Located(offer, "base", info, searched)
    for name in names:
        based = catalog_archive.by_name(conn, name)
        if len(based) == 1:
            return Located(based[0][0], "base", based[0][1], searched)
        if based:
            raise NotLocated("в выгрузке поставщика сегодня её нет, в базе позиций с таким названием их %d — ни одна не взята.\nИскали: %s.\n"
                             "В выгрузке сегодня: нет. В базе позиций: %d.\n%s" % (len(based), searched, len(based),
                                                                                     _variants([(o, i.get("last_stock")) for o, i in based])))
    raise NotLocated("этой позиции нет в выгрузке поставщика и нет в базе позиций.\nИскали: %s.\nВ выгрузке сегодня: нет. В базе позиций: нет." % searched)


def located_args(conn, settings, parsed, ref, found: Located, day: str, account_id=None) -> dict:
    """The keyword arguments of orders.add_item for the position locate found (order_offers.offer_args); a position of the base without a sale
    price says so with what was searched."""
    try:
        return order_offers.offer_args(conn, settings, parsed, ref, found.offer, day, account_id)
    except orders.OrderError as err:
        if found.how != "base":
            raise
        raise NotLocated("позиция «%s» (код %s) есть только в базе позиций, цены продажи для неё нет (%s).\nИскали: %s.\nВ выгрузке сегодня: нет. "
                         "В базе позиций: да, у поставщика последний раз %s." % (found.offer.name_orig or found.offer.name, found.offer.goods_id,
                                                                                str(err).rstrip(".").lower(), found.searched,
                                                                                _day((found.info or {}).get("last_seen"))))


def notes(found: Located, kind: str) -> list:
    """[(kind, text, level)] of the journal records of where an added position came from: the one every reader of the order sees (`kind`, no money
    of the supplier), and the purchase price of a position of the base for the office only (kind «webmim»: a point never reads it,
    routes_orders.point_no_webmim). Nothing for a position found by its code."""
    out = []
    if found.note():
        out.append((kind, found.note(), "yellow" if found.how == "base" else "grey"))
    if found.purchase_note():
        out.append(("webmim", found.purchase_note(), "grey"))
    return out


def write_notes(conn, card_id, records, now: str, author: str) -> None:
    """The records of `notes` written to the order's journal (no commit of its own)."""
    for kind, text, level in records or ():
        orders.add_event(conn, card_id, now, author, kind, text, level)


def suggestions(conn, settings, parsed, ref, query: str, day: str, limit: int = LIMIT, account_id=None) -> List[dict]:
    """What the order shows while a position is typed: up to `limit` positions with a stock, each with its size, stock and the price of its ad
    in the account `account_id` (order_offers.account_for; the main one by default) from the general `settings`."""
    offers = find(conn, parsed, ref, query)[:limit]
    if not offers:
        return []
    only = service._Filtered({o.goods_id: o for o in offers}, getattr(parsed, "issues", {}))
    prices = {r["goods_id"]: r["sale"] for r in economics_view.positions(conn, order_offers.effective_settings(conn, settings, account_id), only, ref, day, terms_store.get_params(conn))}
    prices.update(order_offers.ad_prices(conn, account_id, [o.goods_id for o in offers]))           # the price of the account's published ad first
    return [{"goods_id": o.goods_id, "name": o.name_orig or o.name, "size": "%d/%d R%d" % (o.width, o.height, o.diameter), "stock": o.stock,
             "price": prices.get(o.goods_id)} for o in offers]
