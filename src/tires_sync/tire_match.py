"""One tire at two suppliers: the identity of a tire built from the supplier's fields and its name, before any Avito mapping.

The suppliers write the same tire their own way: 4tochki «IKON 155/80R13 79T Character Ice 7 (Nordman 7) TL (шип.)», WebMIM «Ikon Tyres
155/80/13 T 79 Ikon Character Ice 7 Ш.». The identity is the brand (without «Tyres» and the supplier's `S<N>:`), the model (the brand words
repeated at its start and the service words TL, XL, шип. … dropped; a name in brackets is another name of the same model), the size, the
load and speed indexes, the tire type (season and studs, as the feed writes it), RunFlat and XL. Every field but the model must be equal;
the model of one position must be the main name or a name in brackets of the other (two names in brackets never pair by themselves).
A name that contradicts the fields (another size, studs) makes the position doubtful: it is never paired.

Positions of different suppliers only: two codes of one supplier are the business of same_tire. A group where one supplier has two
positions with different main names is ambiguous and is never merged. The owner adds a pair of names by hand (`match-synonym-add`,
table `model_synonym`); the feed uses the pairs only with the switch `tire_match.feed` on (off by default).

With the switch on the cycle remembers the pairs it found (table `tire_pair`, rows only added): a side that has left the unload is still
paired with the one that is there, so the ad goes on under its Id. The supplier behind an ad of a pair changes at most once a day (the
owner 2026-10-20, table `tire_match_switch`): within a day of the last change the other side waits, the ad is not handed over.

The owner may say two positions are not one tire (`not_a_pair`, table `tire_not_pair`, rows only added): they are never merged, even with
both in the unload, and the memory does not use them either. A group they still meet in through a third position is not merged at all."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Dict, FrozenSet, List, Mapping, Optional, Tuple

from .catalog import lookalike_form, normalize_model
from .feed import _tire_type
from .models import Offer, Season
from .ruwords import BRANDS_RU_TO_EN
from .webmim import studs_named

CONFIG_KIND = "tire_match"            # connections.get_config(conn, "tire_match", 0) = {"feed": true} turns the merge in the feed on
SWITCH_PAUSE = timedelta(days=1)       # the owner 2026-10-20: the supplier behind an ad changes at most once a day

BRAND_TAILS = ("TYRES", "TIRES", "TYRE", "TIRE", "ШИНЫ")
BRAND_ALIASES = {"PIRELLIFORMULA": "FORMULA"}           # brand forms (after the tails are dropped) that name one brand
SERVICE_WORDS = frozenset({"TL", "XL", "RFT", "ZP", "ROF", "SSR", "DSST", "RUNFLAT", "M+S",     # never FR, RF, TT, MS: they tell models apart
                           "ШИП", "Ш", "ШИПЫ", "ШИПОВАННАЯ", "ШИПОВАННЫЕ", "НЕШИП", "НЕШИПОВАННАЯ", "ЛИПУЧКА"})
STOP_SYNONYMS = frozenset({"CHINA", "КИТАЙ", "JAPAN", "ЯПОНИЯ", "RUSSIA", "РОССИЯ", "KOREA", "КОРЕЯ", "EU", "ЕС", "DEMO", "ДЕМО"})
_BRACKETS = re.compile(r"\(([^()]*)\)")
_CYRILLIC = re.compile("[А-ЯЁ]")                      # Cyrillic in brackets is a Russian word («(ТОР)», «(Китай)»), not a model name
_NOT_A_NAME = re.compile(r"^(DOT\d*|MADEIN\w*|\d{4})$|^\d+(SHT|ШT)")
_SIZE = re.compile(r"(?<!\d)(\d{3})\s*/\s*(\d{2})\s*(?:/|Z?R|-|\s)\s*(\d{2})(?!\d)", re.I)
_SUPPLIER = re.compile(r"^s(\d+)-")


@dataclass(frozen=True)
class TireIdentity:
    brand: str
    model: str                         # the main name (normalized form)
    synonyms: FrozenSet[str]           # the names in brackets and the owner's pairs (normalized forms)
    width: int
    height: int
    diameter: int
    load: str
    speed: str
    tire_type: str
    runflat: bool
    xl: bool

    @property
    def bucket(self) -> Tuple:
        return (self.brand, self.width, self.height, self.diameter, self.load, self.speed, self.tire_type, self.runflat, self.xl)


@dataclass
class MatchResult:
    groups: List[Tuple[str, ...]] = field(default_factory=list)           # positions of one tire at two or more suppliers (sorted codes)
    ambiguous: List[Tuple[str, ...]] = field(default_factory=list)        # not merged: one supplier has two different models in them
    refused: List[Tuple[str, ...]] = field(default_factory=list)          # not merged: two of them the owner said are not one tire
    doubtful: Dict[str, str] = field(default_factory=dict)                # code -> why its name contradicts its fields
    rank: Dict[str, int] = field(default_factory=dict)                    # code -> 0 WebMIM row 1, 1 another WebMIM row, 2 4tochki
    identities: Dict[str, TireIdentity] = field(default_factory=dict)
    tochki_supplier: Optional[int] = None
    found: List[Tuple[str, str, str, str]] = field(default_factory=list)  # the pairs found now [(code, code, identity, identity)], to remember
    remembered: List[Tuple[str, ...]] = field(default_factory=list)       # groups with a remembered side that has left the unload
    last_switch: Dict[str, Tuple[str, str, str]] = field(default_factory=dict)   # Id -> (from code, to code, at Moscow) of its last change
    now: str = ""                                                         # Moscow, the clock of the pause
    switches: Dict[str, Tuple[str, str]] = field(default_factory=dict)    # code -> (Id, the code before): goes over to another supplier now
    waiting: Dict[str, Tuple[str, str]] = field(default_factory=dict)     # code -> (Id, not before): waits for the day since the last change

    def rank_of(self, goods_id: str) -> int:
        return supplier_rank(goods_id, self.tochki_supplier)

    def same_change(self, ad_id: str, code: str) -> bool:
        """The ad goes over to `code` again: the change noted last (a cycle whose feed was not published, a draft) is not a new one."""
        last = self.last_switch.get(ad_id)
        return last is not None and last[1] == code

    def wait_until(self, ad_id: str, code: str) -> str:
        """Empty: the ad may go over to `code` (another supplier) now; otherwise the time (Moscow) it may."""
        last = self.last_switch.get(ad_id)
        if not last or not self.now or last[1] == code:
            return ""
        until = datetime.fromisoformat(last[2][:19]) + SWITCH_PAUSE
        return until.isoformat(timespec="seconds") if datetime.fromisoformat(self.now[:19]) < until else ""

    def group_of(self) -> Dict[str, Tuple[str, ...]]:
        return {g: members for members in self.groups for g in members}


@lru_cache(maxsize=65536)
def supplier_of(goods_id: str) -> int:
    match = _SUPPLIER.match(goods_id or "")
    return int(match.group(1)) if match else 1


def supplier_rank(goods_id: str, tochki_supplier: Optional[int]) -> int:
    """The owner 2026-10-20: WebMIM first. Row 1 of WebMIM, then the other WebMIM rows, then 4tochki."""
    sid = supplier_of(goods_id)
    if sid == 1:
        return 0
    return 2 if tochki_supplier and sid == tochki_supplier else 1


@lru_cache(maxsize=65536)
def brand_form(raw: str) -> str:
    """«Ikon Tyres», «IKON», «S2:IKON TYRES» -> «IKON»; Cyrillic lookalikes become Latin, known Russian names English."""
    text = re.sub(r"^S\d+:", "", (raw or "").strip()).upper()
    words = text.split()
    while len(words) > 1 and words[-1] in BRAND_TAILS:
        words.pop()
    form = lookalike_form(" ".join(words))
    form = BRANDS_RU_TO_EN.get(normalize_model(" ".join(words)), form)
    return BRAND_ALIASES.get(form, form)


@lru_cache(maxsize=65536)
def _brand_words(brand_raw: str) -> FrozenSet[str]:
    text = re.sub(r"^S\d+:", "", (brand_raw or "").strip()).upper()
    words = {lookalike_form(w) for w in text.split()} | {lookalike_form(t) for t in BRAND_TAILS}
    return frozenset(w for w in words if w) | {brand_form(brand_raw)}


@lru_cache(maxsize=65536)
def model_form(text: str, brand_raw: str = "") -> str:
    """The normalized main name of a model: brand words at its start and service words dropped («Ikon Character Ice 7 TL» -> CHARACTERICE7)."""
    brand = _brand_words(brand_raw) if brand_raw else frozenset()
    words = [w for w in _BRACKETS.sub(" ", text or "").replace(",", " ").split()]
    while words and lookalike_form(words[0]) in brand:
        words.pop(0)
    kept = [w for w in words if normalize_model(w).rstrip(".").upper() not in SERVICE_WORDS and w not in (".",)]
    return lookalike_form(" ".join(kept)).replace(".", "")


def _bracket_forms(text: str, brand_raw: str) -> FrozenSet[str]:
    if "(" not in (text or ""):
        return frozenset()
    out = set()
    for inner in _BRACKETS.findall(text or ""):
        form = model_form(inner, brand_raw)
        plain = normalize_model(inner)
        if (len(form) < 3 or form.isdigit() or not re.search("[A-Z]", form) or _CYRILLIC.search(inner.upper()) or form in STOP_SYNONYMS
                or plain in STOP_SYNONYMS or _NOT_A_NAME.search(form)):
            continue                                            # «(шип.)», «(XL)», «(2024)», «(Китай)», «(DOT2022)» are not a model's name
        out.add(form)
    return frozenset(out)


def _doubt(offer: Offer) -> str:
    """Why the name contradicts the fields (empty: it does not)."""
    text = " ".join({offer.name or "", offer.name_orig or ""})
    for match in _SIZE.finditer(text):
        if (int(match.group(1)), int(match.group(2)), int(match.group(3))) != (offer.width, offer.height, offer.diameter):
            return "размер в названии %s/%s R%s, в полях %d/%d R%d" % (match.group(1), match.group(2), match.group(3),
                                                                        offer.width, offer.height, offer.diameter)
    if offer.season is Season.WINTER:
        said = studs_named(text)                                # the one set of stud rules (webmim)
        if offer.studded and said is False:
            return "в названии «без шипов», в полях шипы"
        if not offer.studded and said is True:
            return "в названии шипы, в полях без шипов"
    return ""


def identity(offer: Offer, synonyms: Optional[Mapping[Tuple[str, str], str]] = None) -> Optional[TireIdentity]:
    """None: not enough to say two positions are one tire (no brand, model, size, indexes or season)."""
    brand = brand_form(offer.brand_raw or offer.brand_key)
    main = model_form(offer.model_raw, offer.brand_raw or offer.brand_key)
    load, speed = str(offer.load or "").split("/")[0].strip(), str(offer.speed or "").strip().upper()
    tire_type = _tire_type(offer.season, bool(offer.studded))
    if not (brand and main and load and speed and tire_type and offer.width and offer.height and offer.diameter):
        return None
    names = _bracket_forms(offer.model_raw, offer.brand_raw) | _bracket_forms(offer.name, offer.brand_raw) | _bracket_forms(
        offer.name_orig, offer.brand_raw)
    synonyms = synonyms or {}
    canonical = synonyms.get((brand, main))
    names = {synonyms.get((brand, n), n) for n in names} | ({main} if canonical else set())
    main = canonical or main
    return TireIdentity(brand, main, frozenset(n for n in names if n != main), offer.width, offer.height, offer.diameter, load, speed,
                        tire_type, bool(offer.runflat), bool(offer.xl))


def _apart(code: str, members, not_pairs) -> bool:
    """The owner said `code` and one of `members` are not one tire."""
    return bool(not_pairs) and any((min(code, g), max(code, g)) in not_pairs for g in members if g != code)


def pairs(offers: Mapping[str, Offer], synonyms: Optional[Mapping[Tuple[str, str], str]] = None,
          tochki_supplier: Optional[int] = None, memo: Optional[dict] = None, not_pairs=()) -> MatchResult:
    """The groups of one tire at different suppliers among `offers` (every position, in or out of the feed). `memo`: the identities of
    the offers already seen with these synonyms (the panel keeps one; an offer is a frozen record, so its identity never changes).
    `not_pairs`: {(code, code)} (sorted) the owner said are not one tire: never related; a group they meet in all the same is `refused`."""
    not_pairs = frozenset(not_pairs or ())
    result = MatchResult(tochki_supplier=tochki_supplier)
    buckets: Dict[Tuple, List[str]] = {}
    for gid in sorted(offers):
        offer = offers[gid]
        known = memo.get(id(offer)) if memo is not None else None
        if known is not None and known[0] is offer:
            ident, doubt = known[1], known[2]
        else:
            ident = identity(offer, synonyms)
            doubt = _doubt(offer) if ident is not None else ""
            if memo is not None:
                memo[id(offer)] = (offer, ident, doubt)
        if ident is None:
            continue
        if doubt:
            result.doubtful[gid] = doubt
            continue
        result.identities[gid] = ident
        buckets.setdefault(ident.bucket, []).append(gid)
    for members in buckets.values():
        if len({supplier_of(g) for g in members}) < 2:
            continue
        parent = {g: g for g in members}

        def root(g):
            while parent[g] != g:
                parent[g] = parent[parent[g]]
                g = parent[g]
            return g
        by_main: Dict[str, List[str]] = {}
        by_synonym: Dict[str, List[str]] = {}
        for g in members:
            ident = result.identities[g]
            by_main.setdefault(ident.model, []).append(g)
            for name in ident.synonyms:
                by_synonym.setdefault(name, []).append(g)
        for a in members:                                       # related: the same main name, or the main name of one is a name of the other
            ident = result.identities[a]
            near = set(by_main.get(ident.model, ())) | set(by_synonym.get(ident.model, ()))
            for name in ident.synonyms:
                near.update(by_main.get(name, ()))
            for b in near:
                if supplier_of(a) != supplier_of(b) and (min(a, b), max(a, b)) not in not_pairs:
                    parent[root(a)] = root(b)
        comps: Dict[str, List[str]] = {}
        for g in members:
            comps.setdefault(root(g), []).append(g)
        for comp in comps.values():
            if len({supplier_of(g) for g in comp}) < 2:
                continue
            models_of: Dict[int, set] = {}
            for g in comp:
                models_of.setdefault(supplier_of(g), set()).add(result.identities[g].model)
            group = tuple(sorted(comp))
            if any(_apart(g, group, not_pairs) for g in group):
                result.refused.append(group)
                continue
            (result.ambiguous if any(len(m) > 1 for m in models_of.values()) else result.groups).append(group)
    for group in result.groups:
        for g in group:
            result.rank[g] = supplier_rank(g, tochki_supplier)
    result.groups.sort()
    result.ambiguous.sort()
    result.refused.sort()
    return result


def identity_text(ident: TireIdentity) -> str:
    """The identity a pair is remembered with (the fields and the main name)."""
    return json.dumps(list(ident.bucket) + [ident.model], ensure_ascii=False)


def found_pairs(result: MatchResult) -> List[Tuple[str, str, str, str]]:
    """Every two positions of different suppliers of a group found now, with their identities."""
    out = []
    for group in result.groups:
        here = [g for g in group if g in result.identities]
        for i, a in enumerate(here):
            for b in here[i + 1:]:
                if supplier_of(a) != supplier_of(b):
                    out.append((a, b, identity_text(result.identities[a]), identity_text(result.identities[b])))
    return out


def remember(result: MatchResult, stored, offers: Mapping[str, Offer], unavailable=(), not_pairs=()) -> None:
    """Add to the groups the remembered sides that have left the unload (`stored`: store.load_tire_pairs). A pair counts only while the side
    that is here is the same tire as when the pair was found, is not doubtful or ambiguous, and its group has no other position of the
    supplier of the side that is gone (another lot: the business of same_tire). Both sides here: the matching of now decides. A side of a
    supplier that is unavailable now (`unavailable`: its file is old or broken) has not left: its ads are held, the pair is not used.
    A side never joins a group with a position the owner said is not one tire with it (`not_pairs`); a refused group is unclear."""
    not_pairs = frozenset(not_pairs or ())
    groups: List[List[str]] = [list(g) for g in result.groups]
    group_of: Dict[str, int] = {g: i for i, members in enumerate(groups) for g in members}
    unclear = {g for members in result.ambiguous + result.refused for g in members}
    touched: set = set()
    for code_a, code_b, ident_a, ident_b, _found_at in stored:
        if code_a in offers and code_b not in offers:
            here, gone, ident = code_a, code_b, ident_a
        elif code_b in offers and code_a not in offers:
            here, gone, ident = code_b, code_a, ident_b
        else:
            continue
        now = result.identities.get(here)
        if now is None or here in unclear or identity_text(now) != ident or supplier_of(gone) in unavailable:
            continue
        at, there = group_of.get(here), group_of.get(gone)
        if there is not None:                                   # the gone side is in a group already: the side here joins it
            present = [g for g in groups[there] if g in result.identities]
            if at is not None or _apart(here, groups[there], not_pairs) or any(result.identities[g].bucket != now.bucket for g in present) or any(
                    supplier_of(g) == supplier_of(here) and result.identities[g].model != now.model for g in present):
                continue
            groups[there].append(here)
            group_of[here] = there
            touched.add(there)
            continue
        if at is None:
            groups.append([here])
            at = group_of[here] = len(groups) - 1
        if any(supplier_of(g) == supplier_of(gone) for g in groups[at]) or _apart(gone, groups[at], not_pairs):
            continue
        groups[at].append(gone)
        group_of[gone] = at
        touched.add(at)
    if not touched:
        return
    result.groups = sorted(tuple(sorted(m)) for m in groups if len({supplier_of(g) for g in m}) > 1)
    result.remembered = sorted(tuple(sorted(groups[i])) for i in touched)
    for group in result.groups:
        for g in group:
            result.rank[g] = supplier_rank(g, result.tochki_supplier)


def switch_on(conn) -> bool:
    from . import connections
    return connections.get_config(conn, CONFIG_KIND, 0).get("feed") is True


def merge_in_feed(conn) -> bool:
    """The merge works in some feed: the switch `tire_match.feed`, or the 4tochki trial in `merge` for an account (tochki_feed.trial)."""
    from . import tochki_feed
    cfg = tochki_feed.config(conn)
    return switch_on(conn) or bool(cfg["feed"] and cfg["supplier_id"] and cfg["mode"] == "merge" and cfg["accounts"])


def only_webmim_and(result: MatchResult, tochki_supplier: int) -> MatchResult:
    """The merge of the 4tochki trial (owner, question 8): only the groups of WebMIM row 1 and 4tochki; a group with another supplier is
    not merged (a 4tochki position of it then meets the strict entry). The pairs to remember and the remembered groups alike."""
    fits = lambda codes: {supplier_of(g) for g in codes} == {1, tochki_supplier}
    result.groups = [g for g in result.groups if fits(g)]
    result.remembered = [g for g in result.remembered if fits(g)]
    result.found = [p for p in result.found if fits(p[:2])]
    kept = {g for members in result.groups for g in members}
    result.rank = {g: r for g, r in result.rank.items() if g in kept}
    return result


def _loose(brand, width, height, diameter, tire_type, load, speed, runflat) -> Optional[Tuple]:
    """The tire without its model: brand, size, indexes, type (season and studs), RunFlat. None: not enough to tell."""
    load, speed = str(load or "").split("/")[0].strip(), str(speed or "").strip().upper()
    brand = brand_form(brand or "")
    if not (brand and load and speed and tire_type and str(width).strip() and str(diameter).strip()):
        return None
    return (brand, str(width).strip(), str(height).strip(), str(diameter).strip(), str(tire_type).strip(), load, speed, bool(runflat))


def loose_of_offer(offer: Offer) -> Optional[Tuple]:
    return _loose(offer.brand_raw or offer.brand_key, offer.width, offer.height, offer.diameter, _tire_type(offer.season, bool(offer.studded)),
                  offer.load, offer.speed, offer.runflat)


def loose_of_key(key) -> Optional[Tuple]:
    """The same of a same_tire key (a saved or retired ad: Avito brand, model, width, height, diameter, type, load, speed, RunFlat)."""
    if not key or len(key) < 9:
        return None
    brand, _model, width, height, diameter, tire_type, load, speed, runflat = tuple(key)[:9]
    return _loose(brand, width, height, diameter, tire_type, load, speed, runflat)


WITH_TUBE = re.compile(r"\+\s*камер|\bс\s+камер|\bкамер[аы]\b|\btube\b", re.I)    # «шина + камера» (Алтайшина): not our goods (owner 2026-10-27)


def tochki_entry(ev, match: MatchResult, offers: Mapping[str, Offer], published, tochki_supplier: int, mode: str,
                 published_keys=(), retired=(), allow=()) -> Dict[str, str]:
    """The 4tochki positions of the trial that do not go into the feed: {code: why} (plan tochki-feed §4б, §4д, §4е; owner 2026-10-27).

    One tire, one ad. A 4tochki position that is not published yet goes in only when it is plainly a tire of its own: it has an identity,
    it is not doubtful, ambiguous or refused, and WebMIM has not the same tire (brand, size, indexes, type, RunFlat, XL) under any model,
    in the unload in any state (`offers`), on a published ad of this account (`published_keys`: the same_tire keys of its saved ads whose
    positions are not offers now) or on an ad it took off (`retired`, kept for good): `tochki_twin`. In `merge` a position of a group of
    WebMIM and 4tochki goes in (the merge keeps the group one ad under one Id); in `no_twin` it does not: `tochki_pair`. A non-empty
    `allow` (stage 2 of the plan) lets in only its codes: `tochki_not_allowed`. Two 4tochki codes of one tire (Character / Nordman): one,
    the published one, else the one under the owner's main name, else more stock, else the smaller code: `same_tire`. A tire with a
    tube: `with_tube`, published or not. A published 4tochki ad stays otherwise (a new WebMIM twin does not take a live ad off); without
    the merge the WebMIM positions of its group that are not published wait (`same_tire`): two ads of one tire otherwise."""
    prefix = "s%d-" % tochki_supplier
    published = set(published)
    allow = set(allow or ())
    out: Dict[str, str] = {}
    mine = sorted(g for g in ev.ok if g.startswith(prefix))
    for gid in mine:
        offer = ev.ok[gid].offer
        if WITH_TUBE.search("%s %s %s" % (offer.name, offer.name_orig, offer.model_raw)):
            out[gid] = "with_tube"
    by_tire: Dict[Tuple, List[str]] = {}
    for gid in mine:
        ident = match.identities.get(gid)
        if gid not in out and ident is not None:
            by_tire.setdefault(ident.bucket + (ident.model,), []).append(gid)
    for codes in by_tire.values():
        if len(codes) < 2:
            continue
        live = [g for g in codes if g in published]
        if live:
            losers = [g for g in codes if g not in published]
        else:
            def order(g):
                offer = ev.ok[g].offer
                own_name = model_form(offer.model_raw, offer.brand_raw or offer.brand_key) == match.identities[g].model
                return (not own_name, -offer.stock, g)
            losers = sorted(codes, key=order)[1:]
        for g in losers:
            out[g] = "same_tire"
    if mode != "merge":
        for members in match.groups:
            if any(g.startswith(prefix) and g in published and g not in out for g in members):
                for g in members:
                    if not g.startswith(prefix) and g in ev.ok and g not in published:
                        out[g] = "same_tire"
    grouped = {g for members in match.groups for g in members}
    unclear = {g for members in match.ambiguous + match.refused for g in members}
    webmim: set = set()
    for gid, offer in offers.items():
        if not gid.startswith(prefix):
            key = loose_of_offer(offer)
            if key is not None:
                webmim.add(key + (bool(offer.xl),))
    ads = {loose_of_key(k) for k in published_keys} | {loose_of_key(k) for g, i, k, _at in retired      # a 4tochki ad under its own Id is no
           if not (str(g).startswith(prefix) and str(i).split("_")[0] == str(g))}                        # WebMIM; under a WebMIM Id it is
    ads.discard(None)
    for gid in mine:
        if gid in out or gid in published:
            continue
        offer = ev.ok[gid].offer
        ident, key = match.identities.get(gid), loose_of_offer(offer)
        if allow and gid not in allow:
            out[gid] = "tochki_not_allowed"
        elif ident is None or key is None or gid in match.doubtful or gid in unclear:
            out[gid] = "tochki_unclear"
        elif gid in grouped:
            if mode != "merge":
                out[gid] = "tochki_pair"
        elif key + (bool(offer.xl),) in webmim or key in ads:
            out[gid] = "tochki_twin"
    return out


def for_feed(conn, offers: Mapping[str, Offer], account_id: Optional[int] = None, now=None, unavailable=()) -> MatchResult:
    """The pairs the feed merges (the owner's synonyms, 4tochki ranked last), with the remembered ones (not the ones the owner forgot, not for
    a supplier unavailable now);
    for an account the last changes of supplier of its ads (`now`, Moscow: the clock of the pause). Reads only."""
    from . import store, tochki_feed
    apart = frozenset(store.not_tire_pairs(conn))
    result = pairs(offers, store.load_model_synonyms(conn), tochki_feed.config(conn)["supplier_id"], not_pairs=apart)
    result.found = found_pairs(result)
    remember(result, usable_pairs(conn), offers, set(unavailable or ()), apart)
    if account_id is not None:
        result.last_switch = store.last_match_switches(conn, account_id)
    if isinstance(now, datetime):
        result.now = now.replace(tzinfo=None).isoformat(timespec="seconds")
    else:
        result.now = str(now or store.moscow_iso())[:19]
    return result


def record(conn, account_id: int, result: MatchResult, at: str) -> None:
    """After the cycle (the switch on): remember the pairs found and the ads that went over to another supplier."""
    from . import store
    store.add_tire_pairs(conn, result.found, at)
    for code, (ad_id, before) in sorted(result.switches.items()):
        store.add_match_switch(conn, account_id, ad_id, before, code, at)


def usable_pairs(conn) -> List[Tuple[str, str, str, str, str]]:
    """The remembered pairs the memory uses: every stored one but the ones the owner forgot (store.forgotten_tire_pairs) or said are not
    one tire (store.not_tire_pairs)."""
    from . import store
    left_out = set(store.forgotten_tire_pairs(conn)) | set(store.not_tire_pairs(conn))
    return [p for p in store.load_tire_pairs(conn) if (p[0], p[1]) not in left_out]


def forget_pair(conn, code_a: str, code_b: str, author: str, at: str, undo: bool = False) -> bool:
    """The owner: the remembered pair is no longer used by the memory (`undo`: it is again). A mark, the row of the pair stays; the journal
    of actions has it, in one commit. False: nothing done (it is so already). Raises ValueError for a pair that is not remembered."""
    from . import store
    code_a, code_b = sorted((str(code_a).strip(), str(code_b).strip()))
    if not conn.execute("SELECT 1 FROM tire_pair WHERE code_a = ? AND code_b = ?", (code_a, code_b)).fetchone():
        raise ValueError("пары %s | %s нет среди запомненных" % (code_a, code_b))
    if not store.mark_tire_pair(conn, code_a, code_b, not undo, at, author):
        return False
    if undo:
        store.write_audit(conn, at, author, "match_pair_unforget", {"code_a": code_a, "code_b": code_b})
    else:
        store.write_audit(conn, at, author, "match_pair_forget", {"code_a": code_a, "code_b": code_b})
    return True


def not_a_pair(conn, code_a: str, code_b: str, author: str, at: str, undo: bool = False) -> bool:
    """The owner: the two positions are not one tire, they are never merged (`undo`: they may be again). A mark (rows only added) and the
    journal of actions, in one commit. False: nothing done (it is so already). ValueError: not two positions of different suppliers, or an ad
    has gone over within the group the two meet in already (table tire_match_switch, any account; the group: the two and every position
    linked to them by remembered pairs or changes of supplier): the mark takes the whole group apart, so it could change the Id or the
    model of that live ad."""
    from . import store
    code_a, code_b = sorted((str(code_a or "").strip(), str(code_b or "").strip()))
    if not code_a or not code_b:
        raise ValueError("нужны два кода позиций")
    if code_a == code_b:
        raise ValueError("это один и тот же код %s" % code_a)
    if supplier_of(code_a) == supplier_of(code_b):
        raise ValueError("%s и %s — позиции одного поставщика (№%d), склейка их не касается" % (code_a, code_b, supplier_of(code_a)))
    if not undo:
        moves = conn.execute("SELECT account_id, ad_id, from_code, to_code FROM tire_match_switch ORDER BY id").fetchall()
        links = [tuple(r) for r in conn.execute("SELECT code_a, code_b FROM tire_pair")] + [(r[2], r[3]) for r in moves]
        group, grown = {code_a, code_b}, True
        while grown:                                            # both tables are written only with the merge on: small
            grown = False
            for x, y in links:
                if (x in group) != (y in group):
                    group |= {x, y}
                    grown = True
        moved = next((m for m in moves if m[2] in group or m[3] in group), None)
        if moved:
            raise ValueError("объявление Id %s (аккаунт №%d) уже переходило от %s к %s: запрет расклеит группу и может сменить Id или модель "
                             "живого объявления, только по решению владельца" % (moved[1], moved[0], moved[2], moved[3]))
    if not store.mark_not_pair(conn, code_a, code_b, not undo, at, author):
        return False
    if undo:
        store.write_audit(conn, at, author, "match_not_pair_undo", {"code_a": code_a, "code_b": code_b})
    else:
        store.write_audit(conn, at, author, "match_not_pair", {"code_a": code_a, "code_b": code_b})
    return True


def add_synonym(conn, brand_raw: str, alias_raw: str, canonical_raw: str, author: str, at: str) -> Tuple[bool, str, str, str]:
    """The owner's pair «this model is that one» of a brand. Returns (added, brand form, alias form, canonical form)."""
    from . import store
    brand = brand_form(brand_raw)
    alias, canonical = model_form(alias_raw, brand_raw), model_form(canonical_raw, brand_raw)
    added = store.add_model_synonym(conn, brand, alias, canonical, author, at)
    return added, brand, alias, canonical


def cached_pairs(offers_source, synonyms: Mapping[Tuple[str, str], str], tochki_supplier: Optional[int], not_pairs=()) -> MatchResult:
    """`pairs` of one catalog view, kept while it is the same object (the panel serves many requests from one parse) with the same
    synonyms and the same owner's «not a pair» marks (`not_pairs`, as `for_feed` passes them)."""
    not_pairs = frozenset(not_pairs or ())
    named = tuple(sorted(synonyms.items()))
    key = (named, tochki_supplier, tuple(sorted(not_pairs)))
    entry = _cache.get("entry")              # one read: the panel serves several requests at once, never mix two of them
    if entry is not None and entry[0] is offers_source and entry[1] == key:
        return entry[3]
    old = entry[4] if entry is not None and entry[2] == named else None  # an identity depends on the synonyms only
    memo: dict = {}
    if old:                                                       # only the positions of this view are kept: an old unload is let go
        for offer in offers_source.offers.values():
            known = old.get(id(offer))
            if known is not None and known[0] is offer:
                memo[id(offer)] = known
    result = pairs(offers_source.offers, synonyms, tochki_supplier, memo, not_pairs)
    _cache["entry"] = (offers_source, key, named, result, memo)
    return result


_cache: dict = {}
