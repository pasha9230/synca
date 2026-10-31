"""The catalog of a client (a buyer, the owner 2026-10-29; plan client-role §4.3-§6, §13): the rows a client may see and nothing else.

A row is built here from a white list of fields (FIELDS) and the template gets only rows: never an offer, a goods id, a supplier's code,
the supplier's photo address, the purchase price or our retail price. The price is the purchase (the price of the unload) × (1 + the
client's % / 100), rounded up to tens (pricing.final_price). Since 2026-10-31 (the owner) the client's % of a tyre is our % of it (the
feed's general settings: its brand at its supplier, its season, by the diameter; no correction of an account) + the client's shift of
the brand (client_terms); a brand we have no % of is shown only by the client's own absolute %. A % out of −50…300: hidden. Every brand
of the suppliers with stock is shown, in a feed or not (§13 В8). Suppliers are «Склад 1» / «Склад 2» (4tochki)."""
from __future__ import annotations

import hashlib
import hmac
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..catalog import resolve_brand
from ..models import PricingRule
from ..pricing import final_price
from ..suppliers import supplier_of_id
from . import service

FIELDS = ("brand", "model", "name", "size", "season", "studs", "load", "speed", "runflat", "country", "year", "stock", "warehouse", "price",
          "photo")
WAREHOUSE_MAIN, WAREHOUSE_TOCHKI = "Склад 1", "Склад 2"
STOCK_MANY = "40+"                    # 4tochki sends 41 for «more than 40» (the owner 2026-10-29, В3)
SEASONS = {"summer": "Лето", "winter": "Зима", "all": "Всесезонные"}
SORTS = ("brand", "size", "price", "stock")
PAGE = 50
PARAMS = ("size", "q", "s", "t", "wh", "b", "xb", "o", "r", "p", "v")     # the only parameters the links of the page carry on
MAX_TEXT = 120                        # the length of a text filter (the size line, the search)
MAX_SIZES = 20
MAX_BRANDS = 60                       # the brands ticked in one filter
PCT_MIN, PCT_MAX = -50.0, 300.0       # the client's % (the owner 2026-10-31: below the purchase down to −50 %); outside: hidden
SIZE = re.compile(r"(\d{3})\s*/\s*(\d{2})\s*(?:/\s*|[ZzЗз]?\s*[RrРр]\s*)(\d{2})")


def bare_brand(key: str) -> str:
    """The brand key without the supplier's «S<N>:» and case: one brand of both warehouses is one key, and no supplier is in it."""
    return re.sub(r"^S\d+:", "", key or "", flags=re.I).strip().casefold()


Pct = Tuple[Optional[float], Optional[float]]                       # (up to R17, from R18); None: not set


@dataclass(frozen=True)
class Terms:
    ours: Dict[Tuple[str, str], Pct]            # our % by (the brand key of the supplier, the season): the feed's general settings
    rows: Dict[str, tuple]                      # the client's rows by the bare key (client_terms.Row)


def terms_of(conn, login: str) -> Terms:
    """The acting conditions of the client `login` (read on every request: a change acts at once)."""
    from ..client_terms import rows
    from ..settings_store import get_active
    ours = {}
    for key, rule in (get_active(conn)[1].get("pricing") or {}).items():
        brand, _, season = key.rpartition("|")
        if brand and isinstance(rule, dict):
            ours[(brand, season)] = (rule.get("pct_small"), rule.get("pct_large"))
    return Terms(ours, rows(conn, login))


def _pct(value) -> Optional[float]:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    return pct if math.isfinite(pct) and PCT_MIN <= pct <= PCT_MAX else None


def client_price(terms: Terms, offer) -> Optional[int]:
    """The client's price of a position, or None (no % — neither ours nor the client's own —, no purchase, a % out of −50…300): such a
    position is never shown. Our % of the tyre + the client's shift of the brand, or the client's own absolute % (the owner 2026-10-31)."""
    from ..client_terms import EMPTY, client_pct
    if not offer.base_price or offer.base_price <= 0 or not math.isfinite(offer.base_price):
        return None
    diameter = "small" if offer.diameter <= 17 else "large"
    season = getattr(offer.season, "value", offer.season)
    our = terms.ours.get((offer.brand_key, season), (None, None))[0 if diameter == "small" else 1]
    pct = _pct(client_pct(terms.rows.get(bare_brand(offer.brand_key), EMPTY), diameter, None if our is None else float(our)))
    if pct is None:
        return None
    return final_price(offer.base_price, PricingRule(pct, pct), offer.diameter)


def photo_token(secret, goods_id: str) -> str:
    """Our opaque name of a position's photo: no goods id, no code, no address of the supplier in it."""
    key = secret.encode() if isinstance(secret, str) else bytes(secret)
    return hmac.new(key, b"shop-photo:" + goods_id.encode(), hashlib.sha256).hexdigest()[:32]


def priced(terms: Terms) -> bool:
    """Some % acts (ours of a brand or a client's own one): else the catalog is not set up yet («Ассортимент ещё не настроен»)."""
    return bool(terms.ours) or any(r[1] or r[3] for r in terms.rows.values())


@dataclass(frozen=True)
class Visible:
    """A position the client may see: the offer stays here (the server side), only `row` goes to the template."""
    offer: object
    price: int
    warehouse: int                   # 1 or 2
    brand: str                       # the bare key
    brand_label: str


def visible(conn, login: str, offers, tochki_sid, ref=None, brand_map=None) -> List[Visible]:
    """Every position the client may see, before the filters: of any brand of the suppliers (WebMIM and 4 Точки; the owner 2026-10-30),
    with stock and a price."""
    terms = terms_of(conn, login)
    if not priced(terms):
        return []
    labels: Dict[str, str] = {}
    out = []
    for offer in offers:
        brand = bare_brand(offer.brand_key)
        if offer.stock <= 0 or not brand:                               # a tyre with no brand: never a tick without a name
            continue
        price = client_price(terms, offer)
        if price is None:
            continue
        if brand not in labels:
            labels[brand] = resolve_brand(offer.brand_key, ref or {}, brand_map) or (offer.brand_raw or "").strip() or brand.upper()
        warehouse = 2 if tochki_sid and supplier_of_id(offer.goods_id) == tochki_sid else 1
        out.append(Visible(offer, price, warehouse, brand, labels[brand]))
    return out


def parse_sizes(text: str) -> Tuple[List[Tuple[int, int, int]], List[str]]:
    """«205/55R16 195/65 r15 205/55/16» -> the sizes (width, height, diameter) and the parts not understood. Sizes are parted by spaces,
    inside a size «/» is needed («225 45 17» is not understood: a space parts sizes)."""
    text = (text or "")[:MAX_TEXT]
    sizes = []
    for match in SIZE.finditer(text):
        size = tuple(int(g) for g in match.groups())
        if size not in sizes:
            sizes.append(size)
    rest = SIZE.sub(" ", text).split()
    return sizes[:MAX_SIZES], rest[:MAX_SIZES]


def _search_fields(item: Visible) -> tuple:
    """The fields the client's search looks in: the name, the brand, the model and the size; never the code or the goods id."""
    o = item.offer
    size = ["%d/%d R%d" % (o.width, o.height, o.diameter), "%d/%d/%d" % (o.width, o.height, o.diameter),
            "%d %d %d" % (o.width, o.height, o.diameter), "%d%d%d" % (o.width, o.height, o.diameter)]
    fields = [f.lower() for f in [_name(o), item.brand_label, item.brand, o.model_raw or ""] + size if f]
    return fields, [service._compact(f) for f in fields]


@dataclass
class Filters:
    sizes: List[Tuple[int, int, int]]
    unknown: List[str]
    size_text: str
    brands: List[str]
    without: List[str]
    season: str
    studs: str
    warehouse: int
    query: str
    sort: str
    descending: bool
    page: int


def filters_of(args, known_brands) -> Filters:
    """The GET parameters, parsed on the server with limits; a brand the client sees no position of is dropped (never searched for)."""
    size_text = (args.get("size") or "")[:MAX_TEXT]
    sizes, unknown = parse_sizes(size_text)
    pick = lambda name: [b for b in dict.fromkeys(bare_brand(v) for v in args.getlist(name)[:MAX_BRANDS]) if b in known_brands]
    try:
        page = max(1, int(args.get("p") or 1))
    except ValueError:
        page = 1
    return Filters(sizes, unknown, size_text, pick("b"), pick("xb"),
                   args.get("s") if args.get("s") in SEASONS else "", args.get("t") if args.get("t") in ("studded", "friction") else "",
                   {"1": 1, "2": 2}.get(args.get("wh") or "", 0), (args.get("q") or "")[:MAX_TEXT].strip(),
                   args.get("o") if args.get("o") in SORTS else "brand", args.get("r") == "1", page)


def apply(items: List[Visible], f: Filters) -> List[Visible]:
    sizes, brands, without = set(f.sizes), set(f.brands), set(f.without)
    terms = service.search_terms(f.query)
    out = []
    for i in items:
        o = i.offer
        if sizes and (o.width, o.height, o.diameter) not in sizes:
            continue
        if (brands and i.brand not in brands) or i.brand in without:
            continue
        if (f.season and o.season.value != f.season) or (f.studs and o.studded != (f.studs == "studded")):
            continue
        if f.warehouse and i.warehouse != f.warehouse:
            continue
        if terms and not service.fields_match(_search_fields(i), terms):
            continue
        out.append(i)
    keys = {"brand": lambda i: (i.brand_label.casefold(), (i.offer.model_raw or "").casefold(), _size_key(i), i.price),
            "size": lambda i: (_size_key(i), i.brand_label.casefold(), i.price),
            "price": lambda i: (i.price, i.brand_label.casefold(), _size_key(i)),
            "stock": lambda i: (i.offer.stock, i.brand_label.casefold(), _size_key(i))}
    return sorted(out, key=keys[f.sort], reverse=f.descending)


def _size_key(i: Visible):
    return i.offer.diameter, i.offer.width, i.offer.height


def _name(offer) -> str:
    name = " ".join((offer.name_orig or offer.name or "").split())
    if offer.code and len(offer.code) >= 3:                     # a supplier's article written into the name never reaches the client
        name = " ".join(re.sub(re.escape(offer.code), " ", name, flags=re.I).split())
    return name


def row(item: Visible, photo: Optional[str]) -> dict:
    """The only thing the template gets of a position: exactly FIELDS."""
    o = item.offer
    stock = STOCK_MANY if item.warehouse == 2 and o.stock >= service.STOCK_CAP else str(o.stock)
    out = {"brand": item.brand_label, "model": " ".join((o.model_raw or "").split()), "name": _name(o),
           "size": "%d/%d R%d" % (o.width, o.height, o.diameter), "season": SEASONS.get(o.season.value, ""), "studs": bool(o.studded),
           "load": o.load or "", "speed": o.speed or "", "runflat": bool(o.runflat), "country": o.country or "", "year": o.mnf_year or "",
           "stock": stock, "warehouse": WAREHOUSE_TOCHKI if item.warehouse == 2 else WAREHOUSE_MAIN, "price": item.price, "photo": photo}
    return out


def brand_list(items: List[Visible]) -> List[Tuple[str, str]]:
    """(the key, the label) of the brands the client has positions of, by the label."""
    seen = {}
    for i in items:
        seen.setdefault(i.brand, i.brand_label)
    return sorted(seen.items(), key=lambda kv: kv[1].casefold())
