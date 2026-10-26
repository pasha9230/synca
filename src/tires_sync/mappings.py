from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

from . import store
from .candidates import Evaluation
from .catalog import classify_model, normalize_model


class MappingError(Exception):
    pass


@dataclass(frozen=True)
class PendingMapping:
    brand: str
    model_raw: str
    goods_count: int
    goods_ids: List[str]
    suggestions: List[str]
    tier: str = "none"
    proposal: Optional[str] = None


CONFIDENT = ("prefix", "code")


def pending_mappings(ev: Evaluation, ref: Dict[str, FrozenSet[str]]) -> List[PendingMapping]:
    groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    shown: Dict[Tuple[str, str], str] = {}
    for gid, (brand, model) in ev.unmatched.items():
        key = (brand, normalize_model(model))
        groups[key].append(gid)
        shown.setdefault(key, " ".join(model.split()))         # spelling variants (spaces, case) are one model
    result = []
    for key, ids in groups.items():
        brand, model = key[0], shown[key]
        tier, candidates = classify_model(brand, model, ref)
        proposal = candidates[0] if tier in CONFIDENT else None
        result.append(PendingMapping(brand, model, len(ids), sorted(ids), candidates[:3] if tier == "none" else candidates,
                                     tier, proposal))
    result.sort(key=lambda p: (-p.goods_count, p.brand, p.model_raw))
    return result


def confirm_mapping(conn, brand: str, model_raw: str, avito_model: str, ref: Dict[str, FrozenSet[str]],
                    now: str, author: str) -> None:
    if avito_model not in ref.get(brand, frozenset()):
        raise MappingError("Модели «%s» нет у бренда «%s» в справочнике Авито." % (avito_model, brand))
    store.upsert_mapping(conn, brand, model_raw, avito_model)
    store.write_audit(conn, now, author, "mapping_confirm",
                      {"brand": brand, "model_raw": model_raw, "avito_model": avito_model})


def confirm_brand_mapping(conn, brand_key: str, avito_brand: str, ref: Dict[str, FrozenSet[str]],
                          now: str, author: str) -> None:
    brand_key = re.sub(r"^S\d+:", "", brand_key)
    if avito_brand not in ref:
        raise MappingError("Бренда «%s» нет в справочнике Авито." % avito_brand)
    store.upsert_brand_map(conn, brand_key, avito_brand)
    store.write_audit(conn, now, author, "brand_mapping_confirm", {"brand_key": brand_key, "avito_brand": avito_brand})


def is_safe_proposal(avito_brand: str, model_raw: str, candidate: str) -> bool:
    """A confident proposal that is also safe to accept without looking: the supplier's code is the very end of the
    Avito model (or its trailing brackets), or an abbreviation expands to exactly that model. A supplier text that is
    only the beginning of a longer model name (HP vs HP SUV) or sits in the middle is left to the owner."""
    from .catalog import _expand_abbreviation, has_cyrillic, loose_form
    if has_cyrillic(model_raw) and loose_form(model_raw) == loose_form(candidate):
        return True
    wanted = normalize_model(candidate)
    expanded = _expand_abbreviation(avito_brand, model_raw)
    if expanded and normalize_model(expanded) == wanted:
        return True
    raw = normalize_model(model_raw)
    soft = lambda text: re.sub(r"[\u2019'.,()]", "", normalize_model(text))        # apostrophes, dots, brackets only: «+» or «&» change a model
    if raw and soft(model_raw) == soft(candidate):
        return True
    return bool(raw) and wanted.rstrip(")").endswith(raw)


def auto_mappings(conn, parsed, ref, now: str, author: str, apply: bool = False) -> dict:
    """Run every supplier model of every brand against the Avito reference. Safe confident proposals are confirmed
    (when apply); everything else stays as it is: unmatched, ambiguous or confident-but-unsafe ones go to the owner."""
    from .catalog import resolve_brand, resolve_model
    brand_map, confirmed = store.load_brand_map(conn), store.load_confirmed_mappings(conn)
    pairs: Dict[Tuple[str, str], list] = {}
    for offer in parsed.offers.values():
        brand = resolve_brand(offer.brand_key, ref, brand_map)
        if brand is None:
            continue
        item = pairs.setdefault((brand, normalize_model(offer.model_raw)), [brand, " ".join(offer.model_raw.split()), 0])
        item[2] += 1
    accepted, held = [], []
    for brand, model, goods in sorted(pairs.values(), key=lambda p: (p[0], -p[2], p[1])):
        if resolve_model(brand, model, confirmed, ref):
            continue
        tier, found = classify_model(brand, model, ref)
        row = {"brand": brand, "model": model, "goods": goods, "tier": tier, "options": found[:3]}
        if tier in CONFIDENT and len(found) == 1 and is_safe_proposal(brand, model, found[0]):
            accepted.append(dict(row, to=found[0]))
            if apply:
                confirm_mapping(conn, brand, model, found[0], ref, now, author)
        else:
            held.append(row)
    return {"accepted": accepted, "held": held}


def canonical_model(ref, brand: str, typed: str):
    """The Avito spelling of a model typed by hand (case, spaces and dashes do not matter)."""
    wanted = normalize_model(typed)
    if not wanted:
        return None
    for model in sorted(ref.get(brand, frozenset())):
        if normalize_model(model) == wanted:
            return model
    return None


OLD_NAME_LIMIT = 100
HIDDEN_CHARS = re.compile("[\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")   # DEL, C1, invisible and bidi marks


def check_old_model_name(ref, brand: str, model: str, typed: str) -> Tuple[str, str, str]:
    """(Avito brand, Avito model, the name as saved) of a former name, or MappingError; writes nothing."""
    from .description import CONTROL_CHARS
    brand = next((name for name in ref if name.casefold() == (brand or "").strip().casefold()), "")
    if not brand:
        raise MappingError("Выберите бренд из справочника Авито.")
    target = canonical_model(ref, brand, model or "")
    if target is None:
        raise MappingError("Модели «%s» нет у бренда «%s» в справочнике Авито." % ((model or "").strip(), brand))
    if CONTROL_CHARS.search(typed or "") or HIDDEN_CHARS.search(typed or ""):
        raise MappingError("В прежнем названии есть управляющие символы: вставьте текст заново.")
    text = " ".join((typed or "").split())
    if len(text) > OLD_NAME_LIMIT:
        raise MappingError("Прежнее название длиннее %d знаков." % OLD_NAME_LIMIT)
    return brand, target, text


def save_old_model_name(conn, ref, brand: str, model: str, typed: str, now: str, who: str) -> str:
    """Check and save the former name of an Avito model (empty: remove it); returns what the owner reads. MappingError when refused."""
    saved = store.load_old_model_names(conn)
    if not " ".join((typed or "").split()) and (brand, model) in saved:       # a record is cleared even when the reference lost its model
        store.set_old_model_name(conn, brand, model, "", now, who, commit=False)
        store.write_audit(conn, now, who, "old_model_name", {"avito_brand": brand, "avito_model": model, "before": saved[(brand, model)], "after": ""})
        return "Прежнее название убрано: %s %s." % (brand, model)
    brand, target, text = check_old_model_name(ref, brand, model, typed)
    before = saved.get((brand, target), "")
    if before == text:
        return "Без изменений: %s %s." % (brand, target)
    store.set_old_model_name(conn, brand, target, text, now, who, commit=False)
    store.write_audit(conn, now, who, "old_model_name", {"avito_brand": brand, "avito_model": target, "before": before, "after": text})
    return ("Прежнее название сохранено: %s %s → %s." % (brand, target, text)) if text else ("Прежнее название убрано: %s %s." % (brand, target))
