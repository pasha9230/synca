"""«Новые позиции» show only what can matter for publication: goods of brands that are switched off there are left out."""
from __future__ import annotations

from typing import Iterable, List, Set


def disabled_brands(settings, brands: Iterable[str]) -> Set[str]:
    """Brands that are excluded or have no season turned on."""
    on = {brand for brand, _season in settings.enabled}
    return {b for b in set(brands) if b in settings.excluded_brands or b not in on}


def visible(settings, goods: List[dict]) -> List[dict]:
    off = disabled_brands(settings, (g["brand_key"] for g in goods))
    return [g for g in goods if g["brand_key"] not in off]
