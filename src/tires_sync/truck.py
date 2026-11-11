"""The Avito section of commercial tires of type «C»: «Транспорт → Запчасти и аксессуары → Шины, диски и колёса → Шины для грузовиков и
спецтехники», in the feed of the same account. The owner 2026-11-11 (it changes «Все «C»» of 2026-11-10): a «C» whose model is in the passenger
reference (its very name, truck.passenger_model) is published in the passenger section like any passenger tire (passenger_model, resolve_c); only
the others go to this section. Its template and lists are in data/avito-templates
(gruzovye-shiny-2026-11-10.xml: the tags; .json: the values Avito takes for each tag).

A «C» position of this section is matched only against its lists (its Brand and its Model: one list of models for every brand), never against the
passenger reference: a model that is not there waits on «Сопоставления» marked «грузовой раздел». A model with another commercial number
(C4 → C3, C2 → 8) is never taken (catalog.keeps_commercial_mark)."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

from .catalog import classify_model, keeps_commercial_mark, normalize_model, resolve_brand, resolve_model
from .filters import is_commercial_c

TEMPLATES = Path(__file__).resolve().parent / "data" / "avito-templates"
TEMPLATE_PATH = TEMPLATES / "gruzovye-shiny-2026-11-10.xml"
VALUES_PATH = TEMPLATES / "gruzovye-shiny-2026-11-10.json"

PRODUCT_TYPE = "Шины для грузовиков и спецтехники"
SECTION = "truck"                         # the mark of a «C» position on «Сопоставления» (the form's `section`)
SECTION_LABEL = "грузовой раздел"
PASSENGER_LABEL = "легковой раздел"       # a «C» whose model is in the passenger reference (owner, 2026-11-11)
VEHICLE_TYPE = "Грузовая"
CONSTRUCTION = "Радиальный"
TUBE_TYPE = "Бескамерная"


@lru_cache(maxsize=1)
def values() -> Dict[str, FrozenSet[str]]:
    """Tag -> the values Avito takes in this section (the sheet «Справочники» of the template)."""
    raw = json.loads(VALUES_PATH.read_text(encoding="utf-8"))["values"]
    return {tag: frozenset(items) for tag, items in raw.items()}


@lru_cache(maxsize=1)
def template_tags() -> FrozenSet[str]:
    """The tags of the template's sample ad; empty when the file cannot be read (every tag of a truck ad is then «not in the template»)."""
    try:
        sample = ET.parse(str(TEMPLATE_PATH)).getroot().find("Ad")
    except (OSError, ET.ParseError):
        return frozenset()
    return frozenset(child.tag for child in sample) if sample is not None else frozenset()


@lru_cache(maxsize=1)
def reference() -> Dict[str, FrozenSet[str]]:
    """The section's lists in the shape of the passenger reference: every brand of the list with the whole list of models (Avito gives
    the models of this section without their brand), so the brand and the model are found by the very rules of catalog."""
    models = values().get("Model", frozenset())
    return {brand: models for brand in values().get("Brand", ())}


def is_truck_ad(ad: ET.Element) -> bool:
    return (ad.findtext("ProductType") or "").strip() == PRODUCT_TYPE


def resolve(brand_key: str, model_raw: str, confirmed: Dict[Tuple[str, str], str],
            brand_map: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], Optional[str]]:
    """(Avito brand, Avito model) of a «C» position in this section; None where it is not found (resolve_truck_model)."""
    ref = reference()
    brand = resolve_brand(brand_key, ref, brand_map)
    if brand is None:
        return None, None
    return brand, resolve_truck_model(brand, model_raw, confirmed)


PAIR_PREFIX = "truck:"                    # the brand of a pair the owner confirmed for the truck section (the table `mapping`, no new column)


def pair_brand(brand: str) -> str:
    """The brand under which a pair of the truck section is saved: a passenger position never reads it (no passenger brand has the prefix)."""
    return PAIR_PREFIX + brand


def resolve_truck_model(brand: str, model_raw: str, confirmed: Dict[Tuple[str, str], str]) -> Optional[str]:
    """The pair the owner confirmed for the truck section, else the name itself (exact, or with the brand in front, catalog.resolve_model);
    always only a model of this section's list that keeps the commercial number. A passenger pair (saved under the bare brand) is not read:
    a «C» position is no longer matched against the passenger reference."""
    ref = reference()
    norm = normalize_model(model_raw)
    key = (brand, norm)
    saved = confirmed.get((pair_brand(brand), norm))
    usable = {key: saved} if saved is not None and saved in ref.get(brand, ()) else {}
    found = resolve_model(brand, model_raw, usable, ref, commercial=True)
    return found if found is not None and keeps_commercial_mark(model_raw, found) else None


def passenger_model(brand: Optional[str], model_raw: str, ref: Dict[str, FrozenSet[str]]) -> Optional[str]:
    """The model of a «C» position in the passenger reference (owner, 2026-11-11): its very name after normalize_model, as a passenger
    model is found without a pair (catalog: «exact», with the Cyrillic look-alikes, or «prefix», the brand in front), only one, with every
    commercial number of the name. Nothing nearer: no saved pair, no part of a name, no other number. None: the truck section."""
    if not brand:
        return None
    tier, found = classify_model(brand, model_raw, ref, suggestions=False)
    if tier in ("exact", "prefix") and len(found) == 1 and keeps_commercial_mark(model_raw, found[0]):
        return found[0]
    return None


def resolve_c(brand_key: str, model_raw: str, ref: Dict[str, FrozenSet[str]], confirmed: Dict[Tuple[str, str], str],
              brand_map: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], Optional[str], bool]:
    """(Avito brand, Avito model, in the truck section) of a «C» position: the passenger section when its model is in the passenger
    reference (passenger_model), else this section by its lists (resolve)."""
    brand = resolve_brand(brand_key, ref, brand_map)
    model = passenger_model(brand, model_raw, ref)
    if model is not None:
        return brand, model, False
    brand, model = resolve(brand_key, model_raw, confirmed, brand_map)
    return brand, model, True


def in_section(c) -> bool:
    """A candidate (candidates.Candidate) is published in this section: as evaluate decided (resolve_c); a candidate built elsewhere
    without that decision (truck_section None) by its type, as before 2026-11-11."""
    decided = getattr(c, "truck_section", None)
    return decided if decided is not None else is_commercial_c(c.offer)


def sections_of(offers, ref: Dict[str, FrozenSet[str]], confirmed: Dict[Tuple[str, str], str],
                brand_map: Optional[Dict[str, str]] = None) -> list:
    """Every «C» position of the unload with the section it goes to (resolve_c), for a look on the live data (cli `commercial-sections`):
    dicts code, brand_key, model_raw, section («легковой раздел» / «грузовой раздел»), avito_brand, avito_model (None: waits for a pair).
    Reads only; whether the position is published (stock, price, the owner's switches) is not judged here."""
    rows = []
    for code, offer in sorted(offers.items()):
        if not is_commercial_c(offer):
            continue
        brand, model, in_truck = resolve_c(offer.brand_key, offer.model_raw, ref, confirmed, brand_map)
        rows.append({"code": code, "brand_key": offer.brand_key, "model_raw": " ".join(offer.model_raw.split()),
                     "section": SECTION_LABEL if in_truck else PASSENGER_LABEL, "avito_brand": brand, "avito_model": model})
    return rows
