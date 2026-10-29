from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Dict, FrozenSet, Optional, Tuple


class Season(str, Enum):
    WINTER = "winter"
    SUMMER = "summer"
    ALL = "all"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PricingRule:
    pct_small: float  # диаметр до R17 включительно
    pct_large: float  # R18 и выше


@dataclass(frozen=True)
class Offer:
    goods_id: str
    name: str
    name_orig: str
    brand_raw: str
    brand_key: str
    model_raw: str
    season: Season
    width: int
    height: int
    diameter: int
    speed: str
    load: str
    code: str
    stock: int
    base_price: float
    image: str
    runflat: bool
    studded: bool
    car_type: str
    mnf_year: str
    territory_rn: str
    country: str = ""
    homologation: str = ""
    xl: bool = False  # reinforced (XL): WebMIM by the word in the name, 4tochki by the card's `strengthening`; the feed does not read it


@dataclass(frozen=True)
class Settings:
    version: int
    territory_rn: str
    min_stock: int
    hysteresis: int
    min_diameter: int
    portion_limit: int
    stale_days: int
    price_jump_limit: float  # kept in saved settings, no longer used: a supplier's price change is never held
    guard_min: int
    guard_fraction: float
    allow_mass_removal: bool
    pct_limit: float
    date_end_days: int
    date_end_renew_days: int
    contact_phone: str
    point_mode: str  # "percent" или "count"
    point_values: Dict[str, float]
    point_rest: Optional[str]
    enabled: FrozenSet[Tuple[str, Season]]
    pricing: Dict[Tuple[str, Season], PricingRule]
    point_text: Dict[str, str] = field(default_factory=dict)  # адрес текстом вместо идентификатора Авито
    overrides: Dict[str, str] = field(default_factory=dict)  # ручные правила по позициям: "<поставщик>:<товар>" -> on/off
    pack4: bool = False  # второе объявление «за 4 шт.» рядом с обычным (включено, если есть правила)
    pack4_rules: FrozenSet[str] = frozenset()  # "<бренд или *>|<сезон или *>|<радиус или *>": для каких позиций делать комплект
    excluded_sizes: FrozenSet[str] = frozenset()  # "<ключ бренда или *>|<радиус>": бренд (или все бренды поставщика) в этом радиусе не выгружается
    excluded_brands: FrozenSet[str] = frozenset()  # ключи брендов (S<N>: у доп. поставщиков), которые не уходят в выгрузку
    price_overrides: Dict[str, Tuple[str, float]] = field(default_factory=dict)  # своя цена позиции: "<аккаунт>:<товар>" -> ("pct", % к закупке) | ("price", цена)
    own_prices: Dict[str, Tuple[str, float]] = field(default_factory=dict)  # свои цены одного аккаунта по товару: заполняет только connections.effective_settings

    def pack4_for(self, offer) -> bool:
        """Does the second «за 4 шт.» ad apply to this position? Switch on without rules (old settings) means all."""
        if not self.pack4:
            return False
        if not self.pack4_rules:
            return True
        for rule in self.pack4_rules:
            brand, season, diameter = rule.split("|")
            if (brand == "*" or brand == offer.brand_key) and (season == "*" or season == offer.season.value) \
                    and (diameter == "*" or int(diameter) == offer.diameter):
                return True
        return False


class Action(str, Enum):
    PUBLISH = "publish"
    UPDATE = "update"
    KEEP = "keep"
    REMOVE = "remove"
    HOLD = "hold"
    DEFER = "defer"
    SKIP = "skip"


@dataclass(frozen=True)
class PublishedItem:
    goods_id: str
    price: int
    base_price: float
    point: str
    location: str = "supplier"
    first_hold: Optional[date] = None


@dataclass(frozen=True)
class Decision:
    goods_id: str
    action: Action
    reason: str
    price: Optional[int] = None
    point: Optional[str] = None
    first_hold: Optional[date] = None
    suppressed: bool = False


@dataclass
class DecisionSet:
    decisions: list
    guard_tripped: bool
