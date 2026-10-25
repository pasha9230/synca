from __future__ import annotations

import difflib
import html
import re
from functools import lru_cache
from typing import Dict, FrozenSet, List, Optional, Tuple

from .ruwords import BRANDS_RU_TO_EN, translate_words

BRAND_ALIASES = {
    "IKON TYRES": "Ikon Tyres",
    "PIRELLI FORMULA": "Formula",
    "КАМА": "КАМА (Нижнекамский шинный завод)",
}

_MAKE = re.compile(r'^\s*<make name="(.*)">\s*$')
_MODEL = re.compile(r'^\s*<model name="(.*?)"\s*(?:/>|></model>)\s*$')


def parse_reference(text: str) -> Dict[str, FrozenSet[str]]:
    ref: Dict[str, set] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        make = _MAKE.match(line)
        if make:
            current = html.unescape(make.group(1))
            ref.setdefault(current, set())
            continue
        model = _MODEL.match(line)
        if model and current is not None:
            ref[current].add(html.unescape(model.group(1)))
    return {brand: frozenset(models) for brand, models in ref.items()}


def normalize_model(value: str) -> str:
    return re.sub(r"[\s\-_/]+", "", value).upper()


# Cyrillic letters that look like Latin ones: the supplier mixes them in, Avito has the Latin spelling.
_LOOKALIKES = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")
_TRANSLIT = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "E", "Ж": "ZH", "З": "Z", "И": "I", "Й": "Y",
    "К": "K", "Л": "L", "М": "M", "Н": "N", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U", "Ф": "F",
    "Х": "H", "Ц": "TS", "Ч": "CH", "Ш": "SH", "Щ": "SCH", "Ъ": "", "Ы": "Y", "Ь": "", "Э": "E", "Ю": "YU", "Я": "YA",
}
_CYRILLIC = re.compile("[А-ЯЁ]")


def has_cyrillic(value: str) -> bool:
    return bool(_CYRILLIC.search(value.upper()))


def lookalike_form(value: str) -> str:
    """normalize_model with Cyrillic lookalike letters turned into Latin (only those: no guessing of sounds)."""
    return normalize_model(value).translate(_LOOKALIKES)


@lru_cache(maxsize=65536)
def loose_form(value: str) -> str:
    """Sound-based key: Russian spelling and the Latin one meet here (Хакапелиита = Hakkapeliitta, Бизнес = Business).
    Known Russian words are translated first, punctuation is dropped."""
    text = "".join(_TRANSLIT.get(ch, ch) for ch in normalize_model(translate_words(value)))
    text = re.sub(r"[^A-Z0-9+&]", "", text)           # «+» and «&» change a model: they stay
    text = text.replace("CH", "#").replace("KH", "H").replace("CK", "K").replace("C", "K").replace("#", "CH")
    text = text.replace("Y", "I").replace("W", "V")
    return re.sub(r"([A-Z])\1+", r"\1", text)


@lru_cache(maxsize=256)
def _loose_index(models_set: FrozenSet[str]) -> Dict[str, Tuple[str, ...]]:
    index: Dict[str, List[str]] = {}
    for name in sorted(models_set):
        index.setdefault(loose_form(name), []).append(name)
    return {key: tuple(names) for key, names in index.items()}


def _loose_unique(raw: str, candidates) -> List[str]:
    key = loose_form(raw)
    return [name for name in candidates if loose_form(name) == key] if key else []


def resolve_brand(
    brand_key: str, ref: Dict[str, FrozenSet[str]], brand_map: Optional[Dict[str, str]] = None
) -> Optional[str]:
    brand_key = re.sub(r"^S\d+:", "", brand_key)
    if brand_map and brand_map.get(brand_key) in ref:
        return brand_map[brand_key]
    alias = BRAND_ALIASES.get(brand_key)
    if alias is not None and alias in ref:
        return alias
    matches = [name for name in ref if name.upper() == brand_key]
    if not matches and has_cyrillic(brand_key):
        matches = [name for name in ref if lookalike_form(name) == lookalike_form(brand_key)]
        english = BRANDS_RU_TO_EN.get(normalize_model(brand_key))
        if not matches and english:
            for wanted in (english, english + " TYRES", english + " TIRES"):
                matches = [name for name in ref if name.upper() == wanted]
                if matches:
                    break
    if not matches:
        return None
    return sorted(matches, key=lambda n: (-len(ref[n]), n))[0]


def suggest_brand(brand_key: str, ref: Dict[str, FrozenSet[str]], n: int = 3) -> List[str]:
    brand_key = re.sub(r"^S\d+:", "", brand_key)
    names = sorted(ref)
    key_norm = normalize_model(brand_key)
    if has_cyrillic(brand_key):
        same_sound = _loose_unique(brand_key, names)
        if same_sound:
            return same_sound[:n]
    if len(key_norm) >= 3:
        contained = []
        for name in names:
            name_norm = normalize_model(name)
            if len(name_norm) >= 3 and (name_norm in key_norm or key_norm in name_norm):
                contained.append((len(name_norm), name))
        if contained:
            contained.sort(key=lambda item: (-item[0], item[1]))
            return [name for _, name in contained][:n]
    lower = {name.lower(): name for name in names}
    return [lower[m] for m in difflib.get_close_matches(brand_key.lower(), list(lower), n=n, cutoff=0.6)]


@lru_cache(maxsize=256)
def _normalized(models: FrozenSet[str]) -> Tuple[Tuple[str, str], ...]:
    return tuple((m, normalize_model(m)) for m in sorted(models))


def resolve_model(
    avito_brand: str,
    model_raw: str,
    confirmed: Dict[Tuple[str, str], str],
    ref: Dict[str, FrozenSet[str]],
) -> Optional[str]:
    key = (avito_brand, normalize_model(model_raw))
    if key in confirmed:
        return confirmed[key]
    for model, norm in _normalized(ref.get(avito_brand, frozenset())):
        if norm == key[1]:
            return model
    tier, found = classify_model(avito_brand, model_raw, ref, suggestions=False)
    if tier in ("prefix", "exact") and len(found) == 1:
        return found[0]
    return None


MIN_CODE_LENGTH = 4


def classify_model(
    avito_brand: str, model_raw: str, ref: Dict[str, FrozenSet[str]], suggestions: bool = True
) -> Tuple[str, List[str]]:
    """How sure we are about a supplier model: exact / prefix / code (confident), ambiguous, none."""
    tier, found = _classify(ref.get(avito_brand, frozenset()), avito_brand, model_raw, suggestions)
    return tier, list(found)


# The supplier shortens the first word of a model; the long form is what Avito knows.
MODEL_ABBREVIATIONS = {"Pirelli": {"SC": "Scorpion", "SCORP": "Scorpion", "CINT": "Cinturato"}}


def _expand_abbreviation(avito_brand: str, model_raw: str) -> str:
    words = model_raw.split()
    full = MODEL_ABBREVIATIONS.get(avito_brand, {}).get(words[0].upper()) if words else None
    return " ".join([full] + words[1:]) if full else ""


@lru_cache(maxsize=32768)
def _classify(models_set: FrozenSet[str], avito_brand: str, model_raw: str,
              suggestions: bool = True) -> Tuple[str, Tuple[str, ...]]:
    models = _normalized(models_set)
    raw = normalize_model(model_raw)
    exact = [m for m, n in models if n == raw]
    if exact:
        return "exact", tuple(exact)
    if has_cyrillic(model_raw):
        looks = [m for m, n in models if n == lookalike_form(model_raw)]
        if looks:
            return "exact", tuple(looks)
        sounds = list(_loose_index(models_set).get(loose_form(model_raw), ()))
        if len(sounds) == 1:
            return "code", tuple(sounds)          # confident, but the owner accepts it
    expanded = _expand_abbreviation(avito_brand, model_raw)
    if expanded:
        matches = [m for m, n in models if n == normalize_model(expanded)]
        if len(matches) == 1:
            return "code", tuple(matches)         # confident, but the owner accepts it: never applied silently
    brand_norm = normalize_model(avito_brand)
    first_word = normalize_model(avito_brand.split()[0]) if avito_brand.split() else ""
    for prefix in dict.fromkeys(p for p in (brand_norm, first_word) if p):
        if raw.startswith(prefix) and len(raw) > len(prefix):
            rest = raw[len(prefix):]
            stripped = [m for m, n in models if n == rest]
            if stripped:
                return "prefix", tuple(stripped)
    if not has_cyrillic(model_raw) and len(raw) >= MIN_CODE_LENGTH:
        sounds = list(_loose_index(models_set).get(loose_form(model_raw), ()))      # same name, other spelling: apostrophes, doubled letters
        if len(sounds) == 1:
            return "code", tuple(sounds)
    if len(raw) >= MIN_CODE_LENGTH:
        containing = [m for m, n in models if raw in n]
        if len(containing) == 1:
            return "code", tuple(containing)
        if len(containing) > 1:
            return "ambiguous", tuple(containing)
    return "none", tuple(_suggest(models_set, model_raw, 3)) if suggestions else ()


def suggest(avito_brand: str, model_raw: str, ref: Dict[str, FrozenSet[str]], n: int = 3) -> List[str]:
    return list(_suggest(ref.get(avito_brand, frozenset()), model_raw, n))


@lru_cache(maxsize=8192)
def _suggest(models_set: FrozenSet[str], model_raw: str, n: int) -> Tuple[str, ...]:
    models = sorted(models_set)
    raw_norm = normalize_model(model_raw)
    contained = []
    if len(raw_norm) >= 3:
        for model, model_norm in _normalized(models_set):
            if len(model_norm) >= 3 and (model_norm in raw_norm or raw_norm in model_norm):
                contained.append((len(model_norm), model))
    if contained:
        contained.sort(key=lambda item: (-item[0], item[1]))
        return tuple(model for _, model in contained)[:n]
    return tuple(difflib.get_close_matches(model_raw, models, n=n, cutoff=0.5))
