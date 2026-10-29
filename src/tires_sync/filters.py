from __future__ import annotations

from typing import Optional

from .models import Offer, Season, Settings


def owner_exclusion(brand_key: str, diameter: int, excluded_brands, excluded_sizes) -> Optional[str]:
    """Why the owner keeps this brand (or this brand at this radius) out: one rule for the feed and the catalog."""
    if brand_key in excluded_brands:      # the owner may not sell this brand: nothing overrides it
        return "brand_excluded"
    if excluded_sizes:
        prefix = brand_key[: brand_key.index(":") + 1] if brand_key.startswith("S") and ":" in brand_key else ""
        if "%s|%d" % (brand_key, diameter) in excluded_sizes or "%s*|%d" % (prefix, diameter) in excluded_sizes:
            return "size_excluded"
    return None


def is_aged(offer: Offer) -> bool:
    """«старше 3-х/5-ти лет» in the supplier's name or year: the one test behind the old_production exclusion."""
    return "старше" in offer.name.lower() or "старше" in offer.mnf_year.lower()


OTH_GIVES_WAY = frozenset({"brand_excluded", "not_enabled"})


def exclusion_reason(offer: Offer, settings: Settings, on_oth: bool = False) -> Optional[str]:
    """`on_oth`: the position lies on «На ОТХ» (oth_stock): an excluded brand and a season switched off give way, the owner 2026-10-29 —
    the tires are at the point already; every other reason stays."""
    owner = owner_exclusion(offer.brand_key, offer.diameter, () if on_oth else settings.excluded_brands, settings.excluded_sizes)
    if owner:
        return owner
    if offer.car_type.upper() in ("C", "LT"):
        return "commercial"
    if is_aged(offer):
        return "old_production"
    if offer.diameter < settings.min_diameter:
        return "diameter"
    if (offer.brand_key, offer.season) in settings.enabled or (on_oth and offer.season is not Season.UNKNOWN):
        return None
    if offer.season is Season.UNKNOWN and any(b == offer.brand_key for b, _ in settings.enabled):
        return "unknown_season"
    return "not_enabled"
