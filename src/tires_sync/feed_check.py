"""The check of an account's feed before it is published: every rule of `feed_check_rules.json` over every ad.

Pure: the feed, the account's facts (cabinet phone, threshold, Ids of the other accounts, the unload, the reference) go in, findings come out.
A `critical` finding stops the publication of that account's feed (the published one stays), a `warning` is reported."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from .catalog import normalize_model, resolve_brand
from .feed import validate_ad
from .photo_library import is_library_link
from .webmim import NOT_STUDDED

RULES_PATH = Path(__file__).with_name("feed_check_rules.json")
PACK = re.compile(r"-x(\d+)$")
COMMERCIAL = re.compile(r"\b\d{2,3}/\d{2,3}\s*C\b")
STUDS_WORD = re.compile(r"[а-яё]*шип[а-яё]*", re.I)
REISSUE_MARK = re.compile(r"_r\d{8}(?!\d)")                          # a renewal mark of an Id (mirror-reissue, `_r20261020`)
STOCK_SHOWN = re.compile(r"наличии\s*:?\s*(\d+)", re.I)               # «В наличии: 40 шт.» of the description
TOCHKI_REST_SHOWN = 40
TITLE_AGE_WORD = re.compile(r"(?<![^\W\d_])старше(?![^\W\d_])", re.I)   # «старше» as a whole word: not «старшее», but also «(старше)», «старше,»
SUMMARY_BY_TEXT = frozenset({"template_missing"})       # rules summarized by their text, not by a count of ads


@dataclass
class Finding:
    rule: str
    level: str
    ad_id: str
    text: str


@dataclass
class Context:
    """What the check needs to know about the account and the world."""
    mode: str                                          # "generated" or "mirror"
    template_tags: FrozenSet[str]
    reference: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    stocks: Dict[str, int] = field(default_factory=dict)        # supplier code -> stock
    min_stock: int = 4
    phone: str = ""                                    # the cabinet's phone as +7..., "" when it is not to be checked
    other_ids: FrozenSet[str] = frozenset()
    pack4: bool = False
    photo_prefix: str = ""                             # the account's own photo link prefix, "" when not in use
    previous_count: int = 0
    id_code: Dict[str, str] = field(default_factory=dict)  # Id -> the position's code, where an Id belongs to another code (a tire keeps the Id of its oldest ad)
    owner_phone: str = ""                               # the owner's general number: allowed only in the primary account ("" there)
    foreign_addresses: FrozenSet[str] = frozenset()     # the address ids of other accounts and of the general settings that are not this account's own ("" = not judged)
    held: FrozenSet[str] = frozenset()                  # positions kept on their saved snapshot (the supplier does not show them now): not judged by stock
    allowed_photos: FrozenSet[str] = frozenset()        # this account's own library links (approved frames, its own copies); empty when the switch is off
    known_stocks: Dict[str, int] = field(default_factory=dict)  # the stock of positions that are not offers (a row broken elsewhere, a stale supplier)
    template_problem: str = ""                          # why the Avito template cannot be used ("" = it was read): the rule `template_missing`
    tochki_prefix: str = ""                             # «s<N>-» of the 4tochki positions ("" = 4tochki not set up: nothing of it is judged)
    tochki_trial: bool = False                          # the account is in the 4tochki trial (tochki_feed.trial)
    tire_ids: Dict[str, str] = field(default_factory=dict)      # code -> its tire (tire_match.identity_text), for the codes of the feed
    live_models: Dict[str, str] = field(default_factory=dict)   # Id -> Model of the ad Avito reads now (the published file)


def load_rules(path=RULES_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def template_tags_of(path) -> FrozenSet[str]:
    return frozenset(child.tag for child in ET.parse(str(path)).getroot().find("Ad"))


def read_template(path) -> Tuple[FrozenSet[str], str]:
    """The tags of the template's sample ad and "", or an empty set and why the template cannot be used (owner's decision 2026-10-25:
    a feed is not published without the template, the rule `template_missing`)."""
    try:
        root = ET.parse(str(path)).getroot()
    except OSError:
        return frozenset(), "файл не найден или не читается"
    except (ET.ParseError, LookupError, UnicodeError, ValueError):     # LookupError: an encoding Python does not know in the declaration
        return frozenset(), "файл не разбирается как XML"
    sample = root.find("Ad")
    tags = frozenset(child.tag for child in sample) if sample is not None else frozenset()
    return (tags, "") if tags else (frozenset(), "в файле нет объявления-образца")


def _code(ad_id: str) -> str:
    return PACK.sub("", str(ad_id).split("_")[0])


def _phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    return "+7" + digits if len(digits) == 10 else ""


def check_feed(ads: List[ET.Element], ctx: Context, rules: Optional[dict] = None) -> List[Finding]:
    rules = rules or load_rules()
    params, table = rules["params"], rules["rules"]
    out: List[Finding] = []
    code_of = lambda ad_id: ctx.id_code.get(ad_id) or _code(ad_id)

    def add(rule: str, ad_id: str, detail: str = "") -> None:
        level = table[rule][ctx.mode]
        if level != "off":
            out.append(Finding(rule, level, ad_id, (table[rule]["text"] + (": " + detail if detail else ""))))

    if ctx.template_problem:
        add("template_missing", "", ctx.template_problem)
    if not ads:
        add("empty_feed", "")
        return out
    ids = [a.findtext("Id") or "" for a in ads]
    for i in sorted({i for i in ids if ids.count(i) > 1}):
        add("duplicate_id", i)
    for i in sorted({i for i in ids if len(REISSUE_MARK.findall(i)) > 1}):
        add("double_reissue_mark", i)                    # `_r…` goes only on the position's own Id once (sync._own_id): `106_r…_r…` is no cabinet's
    seen: Dict[tuple, str] = {}
    tires: Dict[tuple, str] = {}
    twins: Dict[str, str] = {}
    for ad in ads:
        ad_id = ad.findtext("Id") or ""
        tags = [c.tag for c in ad]
        extra = sorted({t for t in tags if t not in ctx.template_tags})
        if extra:
            add("template_tags", ad_id, ", ".join(extra))
        errors = validate_ad(ad)
        if errors:
            add("required_fields", ad_id, ", ".join(errors))
        pack = PACK.search(ad_id.split("_")[0])
        key = (code_of(ad_id), int(pack.group(1)) if pack else 1)
        if key in seen and seen[key] != ad_id:
            add("duplicate_position", ad_id, "%s и %s" % (seen[key], ad_id))
        seen.setdefault(key, ad_id)
        tire = tuple((ad.findtext(t) or "").strip().upper().replace(" ", "") for t in
                     ("Brand", "Model", "TireSectionWidth", "TireAspectRatio", "RimDiameter", "TireType", "LoadIndex", "SpeedIndex")) + (
            (ad.findtext("RunFlat") or "").strip() == "Да",)
        if tire in tires and tires[tire] != ad_id and code_of(tires[tire]) != code_of(ad_id):
            add("same_tire", ad_id, "%s и %s" % (tires[tire], ad_id))
        tires.setdefault(tire, ad_id)
        if ad_id in ctx.other_ids:
            add("cross_account_id", ad_id)
        if ctx.tochki_prefix:
            _tochki_rules(ad, ad_id, code_of(ad_id), bool(pack), ctx, twins, code_of, add)
        stock = ctx.stocks.get(code_of(ad_id))
        if code_of(ad_id) in ctx.held:
            known = stock if stock is not None else ctx.known_stocks.get(code_of(ad_id))
            if known is not None and known < ctx.min_stock:     # held (the guard, a broken row): the unload says it fell below the threshold
                add("held_low_stock", ad_id, "остаток %d, порог %d" % (known, ctx.min_stock))
        elif stock is None:
            add("stock", ad_id, "нет у поставщика")
        elif stock < ctx.min_stock:
            add("stock", ad_id, "остаток %d, порог %d" % (stock, ctx.min_stock))
        price = (ad.findtext("Price") or "").strip()
        if not price.isdigit() or int(price) <= 0:
            add("price", ad_id, price or "пусто")
        if ctx.phone and _phone(ad.findtext("ContactPhone") or "") != ctx.phone:
            add("phone", ad_id, "%s, у кабинета %s" % (ad.findtext("ContactPhone") or "пусто", ctx.phone))
        if ctx.owner_phone and _phone(ad.findtext("ContactPhone") or "") == ctx.owner_phone:
            add("owner_phone", ad_id)
        address = (ad.findtext("SellerAddressID") or "").strip()
        if address and address in ctx.foreign_addresses:
            add("address", ad_id, "адрес %s чужой (другого аккаунта или общих настроек)" % address)
        if pack and not ctx.pack4:
            add("pack4", ad_id)
        title = ad.findtext("Title") or ""
        if COMMERCIAL.search(title):
            add("commercial", ad_id)
        if ctx.reference:
            brand = (ad.findtext("Brand") or "").strip()
            model = (ad.findtext("Model") or "").strip()
            avito_brand = resolve_brand(brand.upper(), ctx.reference, {}) if brand else None
            if avito_brand is None or normalize_model(model) not in {normalize_model(m) for m in ctx.reference.get(avito_brand, ())}:
                add("model_in_reference", ad_id, "%s / %s" % (brand, model))
        studs = _studs_conflict(ad, title)
        if studs:
            add("studs_conflict", ad_id, studs)
        if len(title) > int(params["title_limit"]) or "  " in title:
            add("title", ad_id, "%d знаков" % len(title))
        if TITLE_AGE_WORD.search(title):                         # the supplier's age note left in a title (owner, 2026-10-25): a warning, publication goes on
            add("title_age_word", ad_id, title)
        image = ad.find("Images/Image")
        if image is None or not image.get("url"):
            add("photo", ad_id, "нет фото")
        elif ctx.photo_prefix and not image.get("url").startswith(ctx.photo_prefix) and image.get("url") not in ctx.allowed_photos:
            add("photo", ad_id, "чужая ссылка")
        for node in ad.findall("Images/Image"):
            url = node.get("url") or ""
            if is_library_link(url) and url not in ctx.allowed_photos:
                add("own_photo_foreign", ad_id, url)
                break
    if ctx.previous_count and len(ads) < ctx.previous_count * (1 - float(params["shrink_warning_fraction"])):
        add("shrink", "", "было %d, стало %d" % (ctx.previous_count, len(ads)))
    return out


def _tochki_rules(ad, ad_id: str, code: str, pack: bool, ctx: Context, twins: Dict[str, str], code_of, add) -> None:
    """The rules of the 4tochki trial (plan tochki-feed §5, owner 2026-10-27): one tire, one ad (`tire_twin`: a pair one side of which is
    4tochki, so the WebMIM feeds are judged as before), no 4tochki outside the trial, at most 40 in the text, the live Model kept."""
    own = code.startswith(ctx.tochki_prefix)            # by the position behind the Id: a WebMIM lot may carry a 4tochki ad's Id (same_tire)
    tire = ctx.tire_ids.get(code)
    if tire and not pack:
        other = twins.get(tire)
        if other is not None and code_of(other) != code and (own or code_of(other).startswith(ctx.tochki_prefix)):
            add("tire_twin", ad_id, "%s и %s" % (other, ad_id))
        twins.setdefault(tire, ad_id)
    if not own:
        return
    if not ctx.tochki_trial and code not in ctx.held:      # held by the guard after the trial was switched off: the guard's alert says it
        add("tochki_outside_trial", ad_id, code)
    shown = STOCK_SHOWN.search(ad.findtext("Description") or "")
    if shown and int(shown.group(1)) > TOCHKI_REST_SHOWN:
        add("tochki_stock", ad_id, shown.group(1))
    model, live = (ad.findtext("Model") or "").strip(), ctx.live_models.get(ad_id)
    if live is not None and live.strip() != model:
        add("tochki_model_change", ad_id, "«%s» → «%s»" % (live, model))


def _studs_conflict(ad: ET.Element, title: str) -> str:
    """What the text says against the studs of `TireType`, "" when nothing. Studded: a negation of webmim.NOT_STUDDED (the reading of the
    unload) in the title or the description, read line by line (its «\\s» would join «под» at the end of a template's line to «шип…» of the
    next) — the mark «Ш.» won the reading while the text says «no studs». Not studded: a «шип…» word left in the title once the negations are
    cut out (the description is not read for it: a template may speak of studded tires in general)."""
    kind = (ad.findtext("TireType") or "").strip()
    if kind == "Зимние шипованные":
        for tag, where in (("Title", "названии"), ("Description", "описании")):
            for line in (ad.findtext(tag) or "").splitlines():
                said = NOT_STUDDED.search(line)
                if said:
                    return "тип «%s», в %s «%s»" % (kind, where, said.group(0).strip())
    elif kind == "Зимние нешипованные":
        said = STUDS_WORD.search(NOT_STUDDED.sub(" ", title))
        if said:
            return "тип «%s», в названии «%s»" % (kind, said.group(0))
    return ""


def critical(findings: Iterable[Finding]) -> List[Finding]:
    return [f for f in findings if f.level == "critical"]


def summarize(findings: List[Finding], limit: int = 3) -> str:
    """One line per rule: «stock: 4 (например 123, 456)». A finding about the whole feed that has no ad (the template) is shown by its text,
    so the panel and the alert say what is wrong."""
    by_rule: Dict[str, List[Finding]] = {}
    for f in findings:
        by_rule.setdefault(f.rule, []).append(f)
    return "; ".join("%s: %s" % (rule, items[0].text) if rule in SUMMARY_BY_TEXT else
                     "%s: %d (например %s)" % (rule, len(items), ", ".join(i.ad_id or "-" for i in items[:limit])) for rule, items in by_rule.items())
