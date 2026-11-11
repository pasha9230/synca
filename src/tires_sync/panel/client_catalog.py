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
from urllib.parse import urlencode

from .. import shop_cart, user_prefs
from ..catalog import resolve_brand
from ..models import PricingRule
from ..pricing import TENS, final_price
from ..suppliers import supplier_of_id
from . import service

FIELDS = ("brand", "model", "name", "size", "season", "studs", "load", "speed", "runflat", "country", "year", "stock", "warehouse", "price",
          "photo", "qty", "qty_max")
QTY_DEFAULT = 4                       # «Количество» of «В корзину»: 4, never more than the stock (the owner 2026-11-09)
# the columns of the client's table a client may hide («Колонки», the owner 2026-11-09); «Шина» and «Количество» always stay
COLUMNS = (("season", "Сезон"), ("country", "Страна"), ("year", "Год"), ("wh", "Склад"), ("stock", "Остаток"), ("price", "Цена за шт."))
COLUMNS_KEY = "shop.cols"             # user_pref of the client: the keys of COLUMNS hidden
WAREHOUSE_MAIN, WAREHOUSE_TOCHKI = "Склад 1", "Склад 2"
STOCK_MANY = "40+"                    # 4tochki sends 41 for «more than 40» (the owner 2026-10-29, В3)
SEASONS = {"summer": "Лето", "winter": "Зима", "all": "Всесезонные"}
SORTS = ("brand", "size", "price", "stock")
PAGE = 50
PARAMS = ("size", "q", "s", "t", "wh", "b", "xb", "o", "r", "p", "v",     # the only parameters the links of the page carry on
          "w", "h", "d", "sf",                                         # «Новый вид»: the width, the height, the radius; the filters folded
          "mixed", "fw", "fh", "fd", "rw", "rh", "rd")                 # «Вид 2»: the mixed-width sets, the front and the rear axis
MAX_VALUES = 30                       # the values of one of the width, the height, the radius ticked at once
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
    return final_price(offer.base_price, PricingRule(pct, pct), offer.diameter, step=TENS)


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
    with stock and a price, of the warehouses switched on for the client (client_terms.warehouses, the owner 2026-11-10). The one place
    that decides it: the catalog, the search, the lists of the filters, the cart, the checkout, the owner's preview and the photos all
    ask here."""
    from ..client_terms import warehouses
    shown = warehouses(conn, login)
    terms = terms_of(conn, login)
    if not priced(terms):
        return []
    labels: Dict[str, str] = {}
    out = []
    for offer in offers:
        brand = bare_brand(offer.brand_key)
        if offer.stock <= 0 or not brand:                               # a tyre with no brand: never a tick without a name
            continue
        warehouse = 2 if tochki_sid and supplier_of_id(offer.goods_id) == tochki_sid else 1
        if warehouse not in shown:
            continue
        price = client_price(terms, offer)
        if price is None:
            continue
        if brand not in labels:
            labels[brand] = resolve_brand(offer.brand_key, ref or {}, brand_map) or (offer.brand_raw or "").strip() or brand.upper()
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


def _search_fields(item: Visible, wide: bool = False) -> tuple:
    """The fields the client's search looks in: the name, the brand, the model and the size; never the code or the goods id. `wide`
    («Новый вид», the owner 2026-10-31 п. 21): the country, the year, the season, the indexes, the studs and RunFlat too."""
    o = item.offer
    size = ["%d/%d R%d" % (o.width, o.height, o.diameter), "%d/%d/%d" % (o.width, o.height, o.diameter),
            "%d %d %d" % (o.width, o.height, o.diameter), "%d%d%d" % (o.width, o.height, o.diameter)]
    more = []
    if wide:
        more = [o.country or "", str(o.mnf_year or ""), SEASONS.get(o.season.value, ""), "%s%s" % (o.load or "", o.speed or ""),
                "шипы" if o.studded else "", "runflat" if o.runflat else ""]
    fields = [f.lower() for f in [_name(o), item.brand_label, item.brand, o.model_raw or ""] + size + more if f]
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
    widths: List[int] = ()
    heights: List[int] = ()
    diameters: List[int] = ()
    wide: bool = False                 # «Новый вид»: the search looks in more fields (_search_fields)


def size_numbers(args, name: str, limit: int = MAX_VALUES) -> List[int]:
    """The values of the width, the height or the radius, one reading for the office's catalog and the client's /shop (the owner
    2026-11-09): repeated (`?w=245&w=255`, the ticks of the list) or typed in one value by spaces or commas («225 205 195», the old
    links), «R16» read as 16. Sorted, each once, `limit` at most."""
    given = args.getlist(name) if hasattr(args, "getlist") else [args.get(name) or ""]
    out = []
    for value in [t for v in given[:limit] for t in re.split(r"[\s,;]+", str(v)[:MAX_TEXT]) if t][:limit]:
        value = value.upper().lstrip("RР")
        if value.isascii() and value.isdigit() and len(value) <= 3 and int(value) not in out:
            out.append(int(value))
    return sorted(out)


SIZE_FIELDS = (("w", "Ширина"), ("h", "Профиль"), ("d", "Диаметр"))
RECENT_KEY = "recent.sizes"           # user_pref of the login: the last sizes searched, the latest first (the owner 2026-11-09)
RECENT_MAX = 5


def _url(base: str, pairs) -> str:
    return base + ("?" + urlencode(pairs) if pairs else "")


def size_pick(base: str, pairs, chosen: dict, options: dict) -> list:
    """The three lists «Ширина», «Профиль», «Диаметр» of the filters (_catalog_filters.html, the office's and the client's): the values
    of the catalog to tick (any chosen one kept even when nothing has it now), the chosen ones as chips each with the link without it,
    the link without any of the field. `pairs`: the parameters of the page; a link never keeps the page number."""
    out = []
    for key, label in SIZE_FIELDS:
        on = list(chosen.get(key) or ())
        rest = [(k, v) for k, v in pairs if k not in (key, "p")]
        out.append({"key": key, "label": label, "options": sorted(set(options.get(key) or ()) | set(on)), "on": on,
                    "chips": [(v, _url(base, rest + [(key, str(x)) for x in on if x != v])) for v in on],
                    "none_url": _url(base, rest) if on else ""})
    return out


def searched_sizes(widths, heights, diameters, sizes) -> List[Tuple[int, int, int]]:
    """The whole sizes a search asked for: those of «Размеры» and the one of the three lists when each holds one value."""
    out = list(sizes or ())
    if len(widths or ()) == 1 and len(heights or ()) == 1 and len(diameters or ()) == 1:
        out.append((widths[0], heights[0], diameters[0]))
    return list(dict.fromkeys(out))


def recent_sizes(conn, login: str) -> List[Tuple[int, int, int]]:
    saved = user_prefs.get(conn, login, RECENT_KEY, []) if login else []
    out = []
    for item in saved if isinstance(saved, list) else []:
        if isinstance(item, list) and len(item) == 3 and all(isinstance(n, int) and 0 < n < 1000 for n in item) and tuple(item) not in out:
            out.append(tuple(item))
    return out[:RECENT_MAX]


def remember_sizes(conn, login: str, searched, now: str) -> List[Tuple[int, int, int]]:
    """«Недавние» of `login`: the sizes just searched go first, the older ones after, RECENT_MAX in all; saved only when it changed."""
    old = recent_sizes(conn, login)
    if not login or not searched:
        return old
    new = list(dict.fromkeys(list(reversed(searched)) + old))[:RECENT_MAX]
    if new != old:
        try:
            user_prefs.put(conn, login, RECENT_KEY, [list(s) for s in new], now)
        except user_prefs.PrefError:
            return old
    return new


def recent_links(base: str, pairs, recent) -> List[Tuple[str, str]]:
    """The chips of «Недавние»: «205/55 R16» and the page with that size in the three lists (the other filters, the view and the sort
    kept; the size fields and the page number replaced)."""
    rest = [(k, v) for k, v in pairs if k not in ("w", "h", "d", "size", "p")]
    return [("%d/%d R%d" % s, _url(base, rest + [("w", s[0]), ("h", s[1]), ("d", s[2])])) for s in recent]


def filters_of(args, known_brands, wide: bool = False) -> Filters:
    """The GET parameters, parsed on the server with limits; a brand the client sees no position of is dropped (never searched for).
    The width, the height and the radius by several values each (both views since 2026-11-09: the filters of the office's catalog);
    `wide` («Новый вид»): the search in more fields."""
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
                   args.get("o") if args.get("o") in SORTS else "brand", args.get("r") == "1", page,
                   size_numbers(args, "w"), size_numbers(args, "h"), size_numbers(args, "d"), wide)


def apply(items: List[Visible], f: Filters) -> List[Visible]:
    sizes, brands, without = set(f.sizes), set(f.brands), set(f.without)
    terms = service.search_terms(f.query)
    out = []
    for i in items:
        o = i.offer
        if sizes and (o.width, o.height, o.diameter) not in sizes:
            continue
        if (f.widths and o.width not in f.widths) or (f.heights and o.height not in f.heights) or (f.diameters and o.diameter not in f.diameters):
            continue
        if (brands and i.brand not in brands) or i.brand in without:
            continue
        if (f.season and o.season.value != f.season) or (f.studs and o.studded != (f.studs == "studded")):
            continue
        if f.warehouse and i.warehouse != f.warehouse:
            continue
        if terms and not service.fields_match(_search_fields(i, f.wide), terms):
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


def row(item: Visible, photo: Optional[str], qty: int = QTY_DEFAULT) -> dict:
    """The only thing the template gets of a position: exactly FIELDS."""
    o = item.offer
    stock = STOCK_MANY if item.warehouse == 2 and o.stock >= service.STOCK_CAP else str(o.stock)
    out = {"brand": item.brand_label, "model": " ".join((o.model_raw or "").split()), "name": _name(o),
           "size": "%d/%d R%d" % (o.width, o.height, o.diameter), "season": SEASONS.get(o.season.value, ""), "studs": bool(o.studded),
           "load": o.load or "", "speed": o.speed or "", "runflat": bool(o.runflat), "country": o.country or "", "year": o.mnf_year or "",
           "stock": stock, "warehouse": WAREHOUSE_TOCHKI if item.warehouse == 2 else WAREHOUSE_MAIN, "price": item.price, "photo": photo}
    most = max(1, min(int(o.stock), shop_cart.MAX_QTY))                 # the cart takes no more (shop_cart.put cuts it there too)
    out.update(qty=min(qty, most), qty_max=most)
    return out


def both_warehouses(conn, login: str) -> bool:
    """Both warehouses are on for the client: only then the filter «Склад», the column «Склад» and its tick in «Колонки» are drawn."""
    from ..client_terms import warehouses
    return len(warehouses(conn, login)) > 1


def columns_of(both: bool) -> tuple:
    return COLUMNS if both else tuple(c for c in COLUMNS if c[0] != "wh")


def hidden_columns(conn, login: str) -> List[str]:
    saved = user_prefs.get(conn, login, COLUMNS_KEY, []) if login else []
    keys = [k for k, _ in COLUMNS]
    return [k for k in keys if isinstance(saved, list) and k in saved]


def save_columns(conn, login: str, shown, now: str) -> List[str]:
    """«Колонки» of the client: every column of COLUMNS not in `shown` is hidden; kept for the login. One warehouse on: the form has
    no tick of «Склад», what was chosen for it stays."""
    shown = set(shown)
    if not both_warehouses(conn, login) and "wh" not in hidden_columns(conn, login):
        shown.add("wh")
    hidden = [k for k, _ in COLUMNS if k not in shown]
    user_prefs.put(conn, login, COLUMNS_KEY, hidden, now)
    return hidden


def brand_list(items: List[Visible]) -> List[Tuple[str, str]]:
    """(the key, the label) of the brands the client has positions of, by the label."""
    seen = {}
    for i in items:
        seen.setdefault(i.brand, i.brand_label)
    return sorted(seen.items(), key=lambda kv: kv[1].casefold())


def size_values(items: List[Visible]) -> dict:
    """The widths, the heights and the radii of the positions the client sees (the chips of «Новый вид»), each sorted."""
    return {"w": sorted({i.offer.width for i in items}), "h": sorted({i.offer.height for i in items}),
            "d": sorted({i.offer.diameter for i in items})}


# «Вид 2» of the client's filters (the owner 2026-11-09…10, by the catalog of the supplier the client knows): the width, the profile,
# the radius as three glued lists of ANY size (not only of the stock), the brands as a list, the mixed-width sets (front / rear).
VIEW_KEY = "shop.fview"               # user_pref of the client: 1 or 2, the view of the block of the filters
QTY_AXIS = 2                          # «Количество» of a tyre of a mixed set: 2 per axis, never more than the stock
KITS_MAX = PAGE                       # the sets shown at most (and the rows of «Только передняя / задняя ось»)
ANY_SIZES = {"w": list(range(125, 400, 10)), "h": list(range(25, 90, 5)), "d": list(range(10, 25))}   # «выбрать любой размер как в мим»
AXES = (("f", "Передняя ось"), ("r", "Задняя ось"))


def filters_view(conn, login: str) -> int:
    """The view of the block of the filters the client chose (1 by default): kept until the client changes it."""
    return 2 if login and user_prefs.get(conn, login, VIEW_KEY, 1) == 2 else 1


def save_filters_view(conn, login: str, value, now: str) -> int:
    view = 2 if str(value) == "2" else 1
    user_prefs.put(conn, login, VIEW_KEY, view, now)
    return view


def size_pick2(base: str, pairs, chosen: dict, options: dict) -> list:
    """The three glued lists of «Вид 2»: any size of ANY_SIZES and of the stock (and the chosen ones), the chosen as chips each with
    the link without it."""
    out = size_pick(base, pairs, chosen, options)
    for p in out:
        p["options"] = sorted(set(ANY_SIZES[p["key"]]) | set(p["options"]))
    return out


def brand_pick(base: str, pairs, items: List[Visible], chosen) -> dict:
    """«Производитель» of «Вид 2»: the brands the client sees (never another's), the chosen as chips each with the link without it."""
    labels = dict(brand_list(items))
    on = [b for b in chosen if b in labels]
    rest = [(k, v) for k, v in pairs if k not in ("b", "p")]
    return {"options": brand_list(items), "on": [(b, labels[b]) for b in on],
            "chips": [(labels[b], _url(base, rest + [("b", x) for x in on if x != b])) for b in on]}


def axis_of(args, prefix: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """One axis of a mixed set (`fw`, `fh`, `fd` or `rw`, `rh`, `rd`): one value each, None (any) when empty."""
    return tuple((size_numbers(args, prefix + k, 1) or [None])[0] for k in ("w", "h", "d"))


def fits_axis(size, axis) -> bool:
    """A size (width, height, diameter) fits an axis of a mixed set: each field empty (None, any) or equal."""
    return all(v is None or v == got for v, got in zip(axis, size))


def _offer_size(o) -> tuple:
    return o.width, o.height, o.diameter


def _fits(item: Visible, axis) -> bool:
    return fits_axis(_offer_size(item.offer), axis)


def offer_model_key(brand, o) -> Optional[tuple]:
    """What the two tyres of a set share: the brand, the model, the season, the studs and RunFlat (the review 2026-11-10). A position
    with no model has no key: never in a set (two «no model» are not one model), only in «Только передняя / задняя ось». One for the
    client's /shop and the office's catalog (lane office-catalog-mim)."""
    model = " ".join((o.model_raw or "").split()).casefold()
    if not model:
        return None
    return brand, model, o.season.value, bool(o.studded), bool(getattr(o, "runflat", False))


def _model_key(item: Visible) -> Optional[tuple]:
    return offer_model_key(item.brand, item.offer)


def mixed_sets(items: List[Visible], f: Filters, front, rear) -> dict:
    """The mixed-width sets (the owner 2026-11-10): a tyre of the front axis and one of the rear of one model (_model_key), of another
    size; one front may be in several sets. A set is ordered: its front of the front axis, its rear of the rear; when both tyres fit
    both axes, the narrower is in front, the mirror is never shown. An empty field is any value, as in MIM (the owner 2026-11-10: no
    radius taken from the front, no width asked for the rear); the sets are grouped by model, so even an empty axis is cheap. At most KITS_MAX + 1
    sets are built (the one past the limit tells «more»). Then the tyres in no set, each once (one fitting both axes: «Только
    передняя»). The other filters (the brands, the season, the studs, the warehouse, the search) act on both axes; the sort is the
    page's."""
    base = apply(items, Filters([], [], "", f.brands, f.without, f.season, f.studs, f.warehouse, f.query, f.sort, f.descending, 1,
                                wide=f.wide))
    return pair_axes(base, lambda i: _offer_size(i.offer), _model_key, front, rear)


def pair_axes(items, size_of, key_of, front, rear) -> dict:
    """The pairing of mixed_sets on any rows (the client's Visible, the office's rows of the catalog): `size_of(row)` its (width, height,
    diameter), `key_of(row)` what the two tyres of a set share (offer_model_key) or None; `items` already filtered and sorted. At most
    KITS_MAX + 1 sets; then the rows of each axis in no set, each once (one fitting both axes: front)."""
    fronts = [i for i in items if fits_axis(size_of(i), front)]
    rears = [i for i in items if fits_axis(size_of(i), rear)]
    order = lambda i: (size_of(i)[2], size_of(i)[0], size_of(i)[1])       # _size_key: the diameter, the width, the height
    by_key: Dict[tuple, list] = {}
    for i in rears:
        key = key_of(i)
        if key is not None:
            by_key.setdefault(key, []).append(i)
    limit = KITS_MAX + 1
    kits, paired = [], set()
    for i in fronts:
        key = key_of(i)
        for j in by_key.get(key, ()) if key is not None else ():
            if j is i or order(i) == order(j):
                continue
            if fits_axis(size_of(j), front) and fits_axis(size_of(i), rear) and order(j) < order(i):
                continue                                          # the mirror (j, i) is the set: the narrower in front
            kits.append((i, j))
            paired.update((id(i), id(j)))
            if len(kits) >= limit:
                break
        if len(kits) >= limit:
            break
    alone_f = [i for i in fronts if id(i) not in paired]
    shown_f = {id(i) for i in alone_f}
    return {"kits": kits, "front": alone_f, "rear": [i for i in rears if id(i) not in paired and id(i) not in shown_f]}
