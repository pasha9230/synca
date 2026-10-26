from __future__ import annotations

import os
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

from .candidates import Candidate
from .description import CONTROL_CHARS, DEFAULT_TEMPLATE, render_description
from .models import Season, Settings

REQUIRED = [
    "Id", "Category", "GoodsType", "AdType", "ProductType", "Condition", "DateEnd", "Title", "Price", "Brand",
    "Model", "TireSectionWidth", "TireAspectRatio", "RimDiameter", "TireType", "SellerAddressID",
    "ContactPhone", "Description",
]
ALLOWED = {
    "Category": {"Запчасти и аксессуары"},
    "GoodsType": {"Шины, диски и колёса"},
    "AdType": {"Товар приобретен на продажу", "Товар от производителя"},
    "ProductType": {"Легковые шины"},
    "Condition": {"Новое", "Б/у"},
    "TireType": {"Зимние шипованные", "Зимние нешипованные", "Летние", "Всесезонные"},
    "MultiItem": {"Да", "Нет"},
    "WholesaleType": {"Да", "Нет"},
    "MinimumOrderQuantity": {"по 2 шт.", "по 4 шт."},
    "FreeTireService": {"Да", "Нет"},
    "RunFlat": {"Да", "Нет"},
}


# The supplier marks tires approved by a carmaker with its code; Avito's «Омологация» takes the carmaker's name from its own list.
HOMOLOGATION = {"BMW": "BMW", "MO": "Mercedes-Benz", "MO1": "Mercedes-Benz", "MOE": "Mercedes-Benz", "N0": "Porsche", "AO": "Audi",
                "L": "Lamborghini", "K1": "Ferrari"}          # K1 (Ferrari): Pirelli/Michelin lists; KS is a structure variant, not a carmaker


def homologation_brand(text: str) -> str:
    """«BMW MO» → «BMW»: the first carmaker the supplier names that Avito's list knows; unknown codes are not written."""
    for word in (text or "").upper().split():
        if word in HOMOLOGATION:
            return HOMOLOGATION[word]
    return ""


ALLOWED["Homologation"] = set(HOMOLOGATION.values())

# The owner, 2026-10-20: «MultiItem да, MinimumOrderQuantity убрать», «снять обозначение продажа оптом, должно быть нет».
MULTI_ITEM = "Да"
WHOLESALE = "Нет"
NOT_SOLD_THIS_WAY = ("MinimumOrderQuantity", "WholesaleMinOrderType", "WholesaleMinOrderCount", "WholesalePacking", "WholesalePackingCount",
                     "WholesaleMeasureUnit", "WholesaleDiscountLadderType", "DiscountLadderList")


class FeedCheckError(Exception):
    pass


def stable_date_end(today: date, days: int, renew: int) -> date:
    ordinal = today.toordinal()
    anchor = date.fromordinal(ordinal - ordinal % renew)
    return anchor + timedelta(days=days)


def _tire_type(season: Season, studded: bool) -> str:
    if season is Season.WINTER:
        return "Зимние шипованные" if studded else "Зимние нешипованные"
    if season is Season.SUMMER:
        return "Летние"
    if season is Season.ALL:
        return "Всесезонные"
    return ""


TITLE_LIMIT = 70


def clean_title(text: str, limit: int = TITLE_LIMIT) -> str:
    """One space between words and at most `limit` signs (cut at a word boundary, a word of its own is never left half)."""
    text = " ".join(CONTROL_CHARS.sub("", str(text or "")).split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if text[limit] != " " and " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,.;-/")


_SIZE = re.compile(r"(?<!\d)(\d{3})/(\d{2,3})/(?:[RrРр])?(\d{2})(?!\d)")
_STUDS = re.compile(r"(?<!\S)[Шш]\.?(?!\S)")
_ORIGIN = re.compile(r"(?<!\S)(KOREA|CN|CHINA|VIETNAM|THAILAND|INDONESIA|JAPAN|TURKEY|RUSSIA|ITALY|GERMANY|FRANCE|SPAIN|FINLAND|SERBIA|POLAND|HUNGARY|ROMANIA|TAIWAN|INDIA|MALAYSIA|USA)\.?(?!\S)\s*$")
IN_STOCK = "в наличии"
AGE_NOTE = re.compile(r"(?<!\S)старше\s+\d+(?:-?(?:х|ти|и|ми|ех))?\s+лет(?!\S)", re.I)   # «старше 3-х лет», «старше 5-ти лет» (supplier's age note)
_AGE_PHRASE = re.compile(r"\(?старше\s+\d+(?:-?(?:х|ти|и|ми|ех))?\s+лет\)?[.,;!]*", re.I)   # the phrase in a form that is not the note:
# «(старше 5-ти лет)», «старше 5-ти лет.» stay supplier's text (owner's narrow list), but a cut takes them off as one
AGE_WORD = re.compile(r"(?<!\S)старше(?!\S)", re.I)                  # what a cut of the note leaves in a title: «… (BMW) старше в наличии»
_PAIR_SEPARATORS = ":-–—"
_PAIR_EDGE = "()[]«»\"'!?.,;"                                        # punctuation around a word, not part of its meaning
_PAIR_SEPARATOR = re.compile(r"[:\-–—]+")                              # a separator standing alone: «Шипы — нет»
_STUDS_KEY = re.compile(r"шипы?", re.I)
_STUDS_VALUE = re.compile(r"нет|да|есть", re.I)
_STUDS_NEGATION = re.compile(r"не|без|нет|под", re.I)


def _glued(left: str, right: str) -> bool:
    """Two neighbouring words of a title that the shortening takes off together: a «feature: value» pair («Шипы: нет», «Шипы — нет»,
    «Шипы -нет», «(Шипы нет)») or a studs negation («без шипов», «под шип»). Half of it would say the opposite («Шипы» alone reads as
    studs and stops the feed by `studs_conflict`). Brackets, quotes and end marks around a word do not count."""
    left, right = left.strip(_PAIR_EDGE), right.strip(_PAIR_EDGE)
    if not left or not right:
        return False
    if left[-1] in _PAIR_SEPARATORS or right[0] in _PAIR_SEPARATORS:
        return True
    if _STUDS_KEY.fullmatch(left) and _STUDS_VALUE.fullmatch(right):
        return True
    return bool(_STUDS_NEGATION.fullmatch(left)) and right.casefold().startswith("шип")


def compose_title(name: str, country: str = "", limit: int = TITLE_LIMIT, tail: str = "") -> str:
    """The title of a tire ad (owner's format, 2026-10-14): «Ikon Tyres 185 55 R15 T 86 Character Ice 5 XL в наличии». The size is written
    «185 55 R15», the studs mark «Ш.» and the supplier's trailing origin code are removed, a word of the brand repeated in the model is dropped
    («Ikon Tyres … Ikon Character» → «Ikon Tyres … Character»), «в наличии» stands at the end. No country (`country` is kept for callers).
    Longer than `limit`: first «XL/FR/RF», then the indexes, then the words of the model from its end, a «feature: value» pair as one
    (`_glued`); the tail always stays. The supplier's age note («старше 5-ти лет», `AGE_NOTE`) is not in the title (owner, 2026-10-25): it stays
    in the description, which shows the supplier's name."""
    text = " ".join(AGE_NOTE.sub(" ", CONTROL_CHARS.sub("", str(name or ""))).split())
    if not text:
        return ""                                                      # no name: no title (the ad is invalid, not «в наличии» alone)
    text = _SIZE.sub(lambda m: "%s %s R%s" % m.groups(), text)
    text = _STUDS.sub("", text)
    text = _ORIGIN.sub("", " ".join(text.split())).strip()
    words = text.split()
    head = []                                                          # the brand words: everything before the size
    for word in words:
        if re.fullmatch(r"\d{3}", word):
            break
        head.append(word)
    brand = {w.casefold() for w in head if w.isalpha()}
    kept, seen_size = [], False
    for word in words:
        if re.fullmatch(r"\d{3}", word) and not seen_size:
            seen_size = True
        elif seen_size and word.isalpha() and word.casefold() in brand:
            continue                                                   # the model repeats the brand
        kept.append(word)
    words = kept
    tail = tail or IN_STOCK
    fits = lambda: len(" ".join(words + [tail])) <= limit
    for extra in ("XL", "FR", "RF"):
        if not fits() and extra in words[4:]:
            words.remove(extra)
    if not fits():
        for i, word in enumerate(words):
            if re.fullmatch(r"R\d{2}", word) and i + 2 < len(words) and re.fullmatch(r"[A-Z]{1,2}", words[i + 1]) and re.fullmatch(r"\d{2,3}(/\d{2,3})?", words[i + 2]):
                del words[i + 1:i + 3]
                break
    floor = max([3] + [i + 1 for i, word in enumerate(words) if re.fullmatch(r"R\d{2}", word)][:1])   # a pair never takes the size
    while words and not fits() and len(words) > 3:
        if len(words) - 3 >= floor and _AGE_PHRASE.fullmatch(" ".join(words[-3:])):
            del words[-3:]                                             # never «… (BMW) старше в наличии»
            continue
        gone = words.pop()
        for _ in range(2):                                             # never half a pair: «… Шипы: нет» loses both words, not «нет»
            if len(words) <= floor or not _glued(words[-1], gone):     # (a pair is at most «key», «separator», «value»)
                break
            gone = words.pop()
        if len(words) > floor and _PAIR_SEPARATOR.fullmatch(words[-1]):
            words.pop()                                                # no separator left hanging: «A - B - C» → «A»
    return " ".join(words + [tail]).strip()


TITLE_FIELDS = ("brand", "model", "size", "indexes", "season", "code", "country")
_TITLE_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def validate_title_template(template: str) -> list:
    errors = []
    if len(template) > 120:
        errors.append("Шаблон заголовка: не длиннее 120 знаков.")
    if CONTROL_CHARS.search(template):
        errors.append("Шаблон заголовка: управляющие символы недопустимы.")
    for name in _TITLE_PLACEHOLDER.findall(template):
        if name not in TITLE_FIELDS:
            errors.append("Шаблон заголовка: неизвестное поле {%s}; доступны: %s." % (name, ", ".join("{%s}" % f for f in TITLE_FIELDS)))
    return errors


def title_from_template(template: str, c: Candidate, tail: str = "", limit: int = TITLE_LIMIT) -> str:
    """The title of an account that has its own template: the fields of the tire in the owner's order. Empty when it does not fit the limit or
    leaves nothing (the standard title is used then)."""
    o = c.offer
    values = {
        "brand": c.avito_brand, "model": c.avito_model, "size": "%s %s R%s" % (o.width, o.height, o.diameter),
        "indexes": " ".join(x for x in (str(o.speed or ""), str(o.load or "")) if x), "season": {"winter": "зимняя", "summer": "летняя", "all": "всесезонная"}.get(str(getattr(o.season, "value", o.season)), ""),
        "code": o.goods_id, "country": str(o.country or "").strip().title(),
    }
    text = _TITLE_PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), template)
    text = " ".join(CONTROL_CHARS.sub("", text).split())
    if tail:
        text = (text + " " + tail).strip()
    return text if text and len(text) <= limit else ""


def render_ad(
    c: Candidate, price: int, point: str, settings: Settings, today: date, template: Optional[str] = None,
    pack: int = 1, title_template: Optional[str] = None, old_models=None,
) -> ET.Element:
    o = c.offer
    ad = ET.Element("Ad")

    def add(name: str, text: str) -> None:
        ET.SubElement(ad, name).text = CONTROL_CHARS.sub("", text)

    add("Id", o.goods_id if pack == 1 else "%s-x%d" % (o.goods_id, pack))
    add("Category", "Запчасти и аксессуары")
    add("GoodsType", "Шины, диски и колёса")
    add("AdType", "Товар приобретен на продажу")
    add("ProductType", "Легковые шины")
    add("Condition", "Новое")
    add("DateEnd", stable_date_end(today, settings.date_end_days, settings.date_end_renew_days).isoformat())
    own_title = title_from_template(title_template, c, "" if pack == 1 else "комплект %d шт." % pack) if title_template else ""
    add("Title", own_title or (compose_title(o.name_orig, o.country) if pack == 1 else compose_title(o.name_orig, "", tail="комплект %d шт." % pack)))
    add("Price", str(price * pack))
    add("Brand", c.avito_brand)
    add("Model", c.avito_model)
    add("TireSectionWidth", str(o.width))
    add("TireAspectRatio", str(o.height))
    add("RimDiameter", str(o.diameter))
    add("MultiItem", MULTI_ITEM)
    add("WholesaleType", WHOLESALE)
    add("FreeTireService", "Нет")
    if settings.point_text.get(point):
        add("Address", settings.point_text[point])
    else:
        add("SellerAddressID", point)
    add("ContactPhone", settings.contact_phone)
    add("TireType", _tire_type(o.season, o.studded))
    if o.runflat:
        add("RunFlat", "Да")
    if o.speed:
        add("SpeedIndex", o.speed)
    if o.load:
        add("LoadIndex", o.load.split("/")[0].strip())
    homologation = homologation_brand(o.homologation)
    if homologation:
        add("Homologation", homologation)
    year = o.mnf_year.strip()
    if year.isdigit() and len(year) == 4 and 2003 <= int(year) <= today.year:     # Avito's list: 1990, 2001, 2003 .. this year
        add("TireYear", year)
    shown = c
    if pack != 1 and o.code:
        shown = replace(c, offer=replace(o, code="%s-x%d" % (o.code, pack)))
    description = render_description(template or DEFAULT_TEMPLATE, shown, price * pack, old_models)
    if pack != 1:
        description = description.replace("за 1 шт.", "за %d шт." % pack)
    add("Description", description)
    if o.image:
        ET.SubElement(ET.SubElement(ad, "Images"), "Image", url=o.image)
    return ad


def validate_ad(ad: ET.Element) -> List[str]:
    errors: List[str] = []
    for name in REQUIRED:
        if name == "SellerAddressID" and (ad.findtext("Address") or "").strip():
            continue
        if not (ad.findtext(name) or "").strip():
            errors.append("empty:%s" % name)
    for name, allowed in ALLOWED.items():
        value = ad.findtext(name)
        if value is not None and value not in allowed:
            errors.append("bad_value:%s" % name)
    if len(ad.findtext("Description") or "") > 7500:
        errors.append("too_long:Description")
    price = ad.findtext("Price") or ""
    if not price.isdigit() or int(price) <= 0:
        errors.append("bad_value:Price")
    return errors


def ad_to_text(ad: ET.Element) -> str:
    return ET.tostring(ad, encoding="unicode")


# Tags an earlier version of the feed wrote and Avito's template of the category no longer has (Avito rejects the ad: «Quantity»).
# A position held on its saved snapshot (the supplier took it away, the protection against a mass removal) comes back with them.
OBSOLETE_TAGS = ("Quantity", "TargetAudience")


def _set_one(ad: ET.Element, name: str, text: str) -> None:
    nodes = ad.findall(name)
    for node in nodes[1:]:
        ad.remove(node)
    node = nodes[0] if nodes else ET.SubElement(ad, name)
    node.text = text


def sale_terms(ad: ET.Element) -> ET.Element:
    """The owner's terms of sale (2026-10-20) on any ad that goes to Avito, ours, saved or carried over from a file:
    `MultiItem` «Да», `WholesaleType` «Нет», no minimum order and none of the wholesale details."""
    for name in NOT_SOLD_THIS_WAY:
        for node in ad.findall(name):
            ad.remove(node)
    _set_one(ad, "MultiItem", MULTI_ITEM)
    _set_one(ad, "WholesaleType", WHOLESALE)
    return ad


def ad_from_text(text: str) -> ET.Element:
    ad = ET.fromstring(text)
    for name in OBSOLETE_TAGS:
        for node in ad.findall(name):
            ad.remove(node)
    return sale_terms(ad)


def assemble_feed(ads: List[ET.Element]) -> bytes:
    root = ET.Element("Ads", formatVersion="3", target="Avito.ru")
    for ad in ads:
        root.append(ad)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def check_feed(count: int, previous_count: int, allow_shrink: bool = False, expected_removals: int = 0) -> None:
    if count == 0:
        raise FeedCheckError("feed is empty")
    explained = count >= previous_count - expected_removals
    if not allow_shrink and previous_count > 0 and count < previous_count * 0.5 and not explained:
        raise FeedCheckError("feed shrank from %d to %d" % (previous_count, count))


def write_atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
