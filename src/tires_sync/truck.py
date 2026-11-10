"""The Avito section of commercial tires of type «C» (owner, 2026-11-10, «Все «C»»): «Транспорт → Запчасти и аксессуары → Шины, диски и
колёса → Шины для грузовиков и спецтехники», in the feed of the same account. Its template and lists are in data/avito-templates
(gruzovye-shiny-2026-11-10.xml: the tags; .json: the values Avito takes for each tag).

A «C» position is matched only against this section's lists (its Brand and its Model: one list of models for every brand), never against the
passenger reference: a model that is not there waits on «Сопоставления» marked «грузовой раздел». A model with another commercial number
(C4 → C3, C2 → 8) is never taken (catalog.keeps_commercial_mark)."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

from .catalog import keeps_commercial_mark, normalize_model, resolve_brand, resolve_model

TEMPLATES = Path(__file__).resolve().parent / "data" / "avito-templates"
TEMPLATE_PATH = TEMPLATES / "gruzovye-shiny-2026-11-10.xml"
VALUES_PATH = TEMPLATES / "gruzovye-shiny-2026-11-10.json"

PRODUCT_TYPE = "Шины для грузовиков и спецтехники"
SECTION = "truck"                         # the mark of a «C» position on «Сопоставления» (the form's `section`)
SECTION_LABEL = "грузовой раздел"
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
