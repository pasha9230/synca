"""The search of a position for an order by its name: «icon tires автограф» finds Ikon Tyres Autograph. Case, Russian or Latin letters, a usual
misspelling and the wrong keyboard layout do not matter. Only positions with a stock are offered, each with the price of its ad."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import List

from . import order_offers, store, terms as terms_store
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


def typed(conn, parsed, ref, text: str):
    """The position a manager typed and sent without choosing from the list: a code of the catalog or an article as it is, or the only position whose
    every word is found exactly. Returns (offer or None, how many positions the text finds), so that a guess by sound is never added silently."""
    text = _clean(text)
    if text in parsed.offers:
        return parsed.offers[text], 1
    by_code = [o for o in parsed.offers.values() if text and o.code and o.code.lower() == text.lower()]
    if len(by_code) == 1:
        return by_code[0], 1
    found = scored(conn, parsed, ref, text)
    exact = [offer for _score_, is_exact, offer in found if is_exact]
    return (exact[0] if len(exact) == 1 and len(found) == 1 else None), len(found)


def suggestions(conn, settings, parsed, ref, query: str, day: str, limit: int = LIMIT, account_id=None) -> List[dict]:
    """What the order shows while a position is typed: up to `limit` positions with a stock, each with its size, stock and the price of its ad
    in the account `account_id` (order_offers.account_for; the main one by default) from the general `settings`."""
    offers = find(conn, parsed, ref, query)[:limit]
    if not offers:
        return []
    only = service._Filtered({o.goods_id: o for o in offers}, getattr(parsed, "issues", {}))
    prices = {r["goods_id"]: r["sale"] for r in economics_view.positions(conn, order_offers.effective_settings(conn, settings, account_id), only, ref, day, terms_store.get_params(conn))}
    return [{"goods_id": o.goods_id, "name": o.name_orig or o.name, "size": "%d/%d R%d" % (o.width, o.height, o.diameter), "stock": o.stock,
             "price": prices.get(o.goods_id)} for o in offers]
