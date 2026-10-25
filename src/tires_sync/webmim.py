from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .brands import MIM_BRAND_TO_KEY
from .models import Offer, Season

_DIGITS = re.compile(r"^\d+$")


COMMERCIAL_TYPES = ("C", "LT")


@dataclass
class ParseResult:
    offers: Dict[str, Offer] = field(default_factory=dict)
    issues: Dict[str, List[str]] = field(default_factory=dict)
    other_territories: Dict[str, int] = field(default_factory=dict)
    unavailable: set = field(default_factory=set)  # suppliers whose unload could not be used this time
    commercial_skipped: int = 0                    # commercial tires (C / LT): not passenger tires, never taken from the unload
    stocks: Dict[str, int] = field(default_factory=dict)   # the known stock of positions that are not offers (a row with an error elsewhere, a stale supplier)


def season_from_text(text: str) -> Season:
    t = text.lower()
    if "всесез" in t:
        return Season.ALL
    if "зим" in t:
        return Season.WINTER
    if "лет" in t:
        return Season.SUMMER
    return Season.UNKNOWN


_RUNFLAT_WORDS = re.compile(r"(?<![0-9A-ZА-Я])(RUN[ \-]?FLAT|RFT|ZP|ROF|SSR|DSST)(?![0-9A-ZА-Я])", re.I)


def runflat_in_text(text: str) -> bool:
    """RunFlat is named in the position's name (RunFlat, Run-Flat, RFT, ZP, ROF, SSR, DSST) as a word of its own."""
    return bool(_RUNFLAT_WORDS.search(text or ""))


_XL_WORDS = re.compile(r"(?<![A-ZА-ЯЁ])(XL|ХL|EXTRA[ \-]?LOAD|REINFORCED)(?![0-9A-ZА-ЯЁ])", re.I)


def xl_in_text(text: str) -> bool:
    """A reinforced tire (XL, Extra Load, Reinforced) is named in the position's name as a word of its own or after the index («94XL»; «XXL» is not it)."""
    return bool(_XL_WORDS.search(text or ""))


# The one set of stud rules (run «match3»): the feed (here), the doubtful positions of tire_match and the line a client waits for read from
# a chat title (order_wait) take it from here. A negation holds «шип» too, so the negations are cut out first; «липучка», «фрикцион» and
# «friction» say «no studs» and hold no «шип» nor «ш.»: cut out as bare stems (no ending, which could hold «ш»), they change no answer of
# `studded_in_text`.
NOT_STUDDED = re.compile(r"(?<![а-яё])(?:(?:не|без|нет|под)\s*шип[а-яё]*|шипуем[а-яё]*|шипы?\s*[:\-–—]?\s*нет(?![а-яё])"
                         r"|липучк|фрикцион|friction)\.?", re.I)


def studs_named(text: str) -> Optional[bool]:
    """What a text says of studs: True — studs («шип…» or the WebMIM mark «Ш.» left once the negations are cut out; the mark wins over a
    negation), False — a negation only («нешип…», «не шип…», «без шип…», «нет шип…», «под шип…», «шипуем…» (made for studs, sold without
    them), «Шипы: нет», «липучка», «фрикцион», «friction»), None — nothing of studs."""
    low = (text or "").lower()
    rest = NOT_STUDDED.sub(" ", low)
    if "шип" in rest or "ш." in rest:
        return True
    return False if rest != low else None


def studded_in_text(text: str) -> bool:
    """Studs in the name or the season of a WebMIM row (`studs_named` is True): a negation reads as no studs, the supplier's own mark «Ш.»
    still wins."""
    return studs_named(text) is True


def brand_key(raw: str) -> str:
    upper = raw.strip().upper()
    return MIM_BRAND_TO_KEY.get(upper, upper)


def _text(node, tag: str) -> str:
    return (node.findtext(tag) or "").strip()


def _int(raw: str, name: str, problems: List[str]) -> int:
    if _DIGITS.match(raw):
        return int(raw)
    problems.append("bad_%s:%s" % (name, raw))
    return 0


def merge_parsed(parts: List["ParseResult"]) -> "ParseResult":
    merged = ParseResult()
    other: Counter = Counter()
    for part in parts:
        merged.unavailable |= part.unavailable
        for gid in part.offers:
            if gid in merged.offers:
                merged.issues.setdefault(gid, []).append("duplicate_id_between_suppliers")
        merged.offers.update(part.offers)
        merged.stocks.update(part.stocks)
        for gid, items in part.issues.items():
            merged.issues[gid] = list(items)
        other.update(part.other_territories)
    merged.other_territories = dict(other)
    return merged


def _derived_id(node) -> str:
    """A position without its own number gets an id from its content, so that it stays the same between unloads."""
    basis = "|".join(_text(node, name) for name in ("SMARKA", "SMODIFNAME_ORIG", "SMODIFNAME", "SMNFCODE", "SSEASON"))
    return "x-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def parse_unload(xml_bytes: bytes, territory_rn: str, supplier: int = 1) -> ParseResult:
    root = ET.fromstring(xml_bytes)
    result = ParseResult()
    other: Counter = Counter()
    seen = set()
    dupes = set()
    for index, node in enumerate(root.iter("COMMODITY")):
        territory = _text(node, "TERRITORY_RN")
        if territory != territory_rn:
            other[territory] += 1
            continue
        if _text(node, "SCARTYPE").strip().upper() in COMMERCIAL_TYPES:
            result.commercial_skipped += 1               # owner's decision 2026-10-14: they are not passenger tires and do not pass the mapping
            continue
        gid = _text(node, "NNOMMODIF") or _derived_id(node)
        if supplier != 1:
            gid = "s%d-%s" % (supplier, gid)
        if gid in seen:
            dupes.add(gid)
            continue
        seen.add(gid)
        problems: List[str] = []
        diameter = _int(_text(node, "SDIAMETR"), "diameter", problems)
        width = _int(_text(node, "SWIDTH"), "width", problems)
        height = _int(_text(node, "SHEIGHT"), "height", problems)
        stock = _int(_text(node, "NREST"), "stock", problems)
        raw_price = _text(node, "NPRICE_DELAYED")
        try:
            price = float(raw_price.replace(",", "."))
            if not math.isfinite(price):
                raise ValueError(raw_price)
        except ValueError:
            price = 0.0
            problems.append("bad_price:%s" % raw_price)
        if problems:
            result.issues[gid] = problems
            if not any(p.startswith("bad_stock:") for p in problems):
                result.stocks[gid] = stock             # the row is broken elsewhere, its stock is known: a fall below the threshold takes it off
            continue
        name = _text(node, "SMODIFNAME")
        name_orig = _text(node, "SMODIFNAME_ORIG") or name
        season_text = _text(node, "SSEASON")
        text = ("%s %s %s" % (name, name_orig, season_text)).lower()
        raw_brand = _text(node, "SMARKA")
        result.offers[gid] = Offer(
            goods_id=gid, name=name, name_orig=name_orig, brand_raw=raw_brand, brand_key=brand_key(raw_brand) if supplier == 1 else "S%d:%s" % (supplier, brand_key(raw_brand)),
            model_raw=_text(node, "SMODEL"), season=season_from_text(season_text), width=width, height=height,
            diameter=diameter, speed=_text(node, "SSPEED"), load=_text(node, "SLOAD"),
            code=_text(node, "SMNFCODE"), stock=stock, base_price=price, image=_text(node, "SPICTURE"),
            runflat=_text(node, "SRUNFLAT").casefold() not in ("", "нет", "no", "0", "-", "false") or runflat_in_text("%s %s %s" % (name_orig, name, _text(node, "SMODEL"))), studded=studded_in_text(text),
            car_type=_text(node, "SCARTYPE"), mnf_year=_text(node, "SMNFYEAR"), territory_rn=territory,
            country=_text(node, "SBRANDCOUNTRY"), homologation=_text(node, "SHOMOLOGATION"),
            xl=xl_in_text("%s %s %s" % (name, name_orig, _text(node, "SMODEL"))),
        )
    for gid in dupes:
        result.offers.pop(gid, None)
        result.stocks.pop(gid, None)                   # two rows under one number: neither stock is known for sure
        result.issues.setdefault(gid, []).append("duplicate_in_territory")
    result.other_territories = dict(other)
    return result
