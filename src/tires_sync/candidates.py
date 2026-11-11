from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from . import truck
from .catalog import resolve_brand, resolve_model
from .filters import exclusion_reason, is_commercial_c
from .models import Offer, Settings
from .overrides import FORCEABLE, override_key
from .pricing import position_price


@dataclass(frozen=True)
class Candidate:
    goods_id: str
    offer: Offer
    price: int
    avito_brand: str
    avito_model: str
    truck_section: Optional[bool] = None    # a «C» in the truck section (truck.resolve_c); None: not decided here, by its type (truck.in_section)


@dataclass
class Evaluation:
    ok: Dict[str, Candidate] = field(default_factory=dict)
    problems: Dict[str, List[str]] = field(default_factory=dict)
    excluded: Dict[str, str] = field(default_factory=dict)
    unmatched: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    stocks: Dict[str, int] = field(default_factory=dict)
    forced: Set[str] = field(default_factory=set)
    aliases: Dict[str, str] = field(default_factory=dict)      # code -> its alias Id (accounts with the alias scheme); built each cycle, saved only for published codes
    inherit: Dict[str, str] = field(default_factory=dict)      # code -> the Id of the oldest ad of the same tire (see same_tire.py)
    match: Optional[object] = None                             # tire_match.MatchResult the cycle merged by (the switch on); None: off
    oth: Dict[str, int] = field(default_factory=dict)          # code -> pieces on «На ОТХ» for this account (sync.plan_decisions): kept, «Шины в наличии»
    oth_points: Dict[str, Tuple[str, ...]] = field(default_factory=dict)   # code -> the «Где» of its points on OTH (the first line)
    oth_gave: Dict[str, str] = field(default_factory=dict)     # code -> the lot it handed its OTH over to (one tire, one ad): that ad names its points too
    oth_retired: Dict[str, Tuple[str, str, int]] = field(default_factory=dict)   # code -> (former Id, saved ad, price): published again from it
    oth_offers: Dict[str, Offer] = field(default_factory=dict)  # code -> its last offer from the archive (gone, «Свой остаток» and an own price)
    oth_carried: Dict[str, str] = field(default_factory=dict)   # code -> the lot of the same tire whose ad shows its «Свой остаток»
    oth_no_id: Dict[str, str] = field(default_factory=dict)     # code -> "never" (not published here before) or its former other Id: not sent
    oth_no_price: Dict[str, int] = field(default_factory=dict)  # code -> manual pieces not counted in this account: gone, no own price
    oth_price: Dict[str, int] = field(default_factory=dict)    # code -> own price of a gone position on «Свой остаток» held or back on its saved ad
    held: Dict[str, Tuple[float, float, str]] = field(default_factory=dict)  # code -> (anchor, supplier's price, why): priced from the anchor (price_guard)
    truck: Set[str] = field(default_factory=set)               # unmatched codes of «C» positions (no passenger model): they wait for a model of the truck section


def evaluate(
    offers: Dict[str, Offer],
    issues: Dict[str, List[str]],
    settings: Settings,
    ref: Dict[str, FrozenSet[str]],
    confirmed: Dict[Tuple[str, str], str],
    brand_map: Optional[Dict[str, str]] = None,
    oth: FrozenSet[str] = frozenset(),
) -> Evaluation:
    """`oth`: the codes on «На ОТХ» (oth_stock): their excluded brand or season switched off does not keep them out (filters)."""
    ev = Evaluation()
    for gid, items in issues.items():
        ev.problems[gid] = list(items)
    for gid, offer in offers.items():
        ev.stocks[gid] = offer.stock
        reason = exclusion_reason(offer, settings, on_oth=gid in oth)
        manual = settings.overrides.get(override_key(gid))
        if manual == "off":
            ev.excluded[gid] = "manual_off"
            continue
        if manual == "on":
            ev.forced.add(gid)
            if reason in FORCEABLE:
                reason = None
        if reason == "unknown_season":
            ev.problems[gid] = ["unknown_season"]
            continue
        if reason is not None:
            ev.excluded[gid] = reason
            continue
        problems: List[str] = []
        price = 0
        if (offer.brand_key, offer.season) not in settings.pricing and gid not in settings.own_prices:
            problems.append("no_pricing_rule")
        elif offer.base_price <= 0:
            problems.append("bad_price:%s" % offer.base_price)
        else:
            price = position_price(offer, settings)                          # its own price in this account first, else the brand's
        truck_section = False
        if is_commercial_c(offer):                                           # «C»: the passenger model, else the truck section (truck.py)
            avito_brand, avito_model, truck_section = truck.resolve_c(offer.brand_key, offer.model_raw, ref, confirmed, brand_map)
        else:
            avito_brand = resolve_brand(offer.brand_key, ref, brand_map)
            avito_model = resolve_model(avito_brand, offer.model_raw, confirmed, ref) if avito_brand is not None else None
        if avito_brand is None:
            problems.append("brand_not_in_reference:%s" % offer.brand_key)
        elif avito_model is None:
            problems.append("model_needs_mapping:%s" % offer.model_raw)
            ev.unmatched[gid] = (avito_brand, offer.model_raw)
            if truck_section:
                ev.truck.add(gid)
        if problems:
            ev.problems[gid] = problems
        else:
            ev.ok[gid] = Candidate(gid, offer, price, avito_brand, avito_model, truck_section)
    return ev
