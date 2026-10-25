from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Dict

from .overrides import VALID
from .models import PricingRule, Season, Settings


def load_env(path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def settings_from_dict(raw: dict, enforce_pct_limit: bool = True) -> Settings:
    enabled = frozenset((brand, Season(season)) for brand, season in raw["enabled"])
    pricing = {}
    for key, value in raw["pricing"].items():
        brand, season = key.split("|")
        pricing[(brand, Season(season))] = PricingRule(float(value["pct_small"]), float(value["pct_large"]))
    pack4_rules = _pack4_rules(raw.get("pack4_rules"))
    settings = Settings(
        version=int(raw["version"]),
        territory_rn=str(raw["territory_rn"]),
        min_stock=int(raw["min_stock"]),
        hysteresis=0,           # решение владельца: снимаем при остатке меньше порога, возвращаем при остатке не меньше порога
        min_diameter=int(raw["min_diameter"]),
        portion_limit=int(raw["portion_limit"]),
        stale_days=int(raw["stale_days"]),
        price_jump_limit=float(raw["price_jump_limit"]),
        guard_min=int(raw["guard_min"]),
        guard_fraction=float(raw["guard_fraction"]),
        allow_mass_removal=bool(raw["allow_mass_removal"]),
        pct_limit=float(raw["pct_limit"]),
        date_end_days=int(raw["date_end_days"]),
        date_end_renew_days=int(raw["date_end_renew_days"]),
        contact_phone=str(raw["contact_phone"]),
        point_mode=str(raw["point_mode"]),
        point_values={str(k): float(v) for k, v in raw["point_values"].items()},
        point_rest=raw.get("point_rest"),
        enabled=enabled,
        pricing=pricing,
        pack4=bool(pack4_rules) if "pack4_rules" in raw else bool(raw.get("pack4", False)),
        pack4_rules=pack4_rules,
        overrides=_overrides(raw.get("overrides")),
        excluded_brands=_excluded(raw.get("excluded_brands")),
        excluded_sizes=_excluded_sizes(raw.get("excluded_sizes")),
    )
    _validate(settings, enforce_pct_limit)
    return settings


def _excluded(raw) -> frozenset:
    raw = [] if raw is None else raw
    if not isinstance(raw, list) or any(not isinstance(b, str) or not b.strip() for b in raw):
        raise ValueError("excluded_brands: нужен список названий брендов")
    return frozenset(raw)


_SIZE_RULE = re.compile(r"^(S\d+:)?(\*|[^|*]+)\|\d{1,2}$")


def _excluded_sizes(raw) -> frozenset:
    raw = [] if raw is None else raw
    if not isinstance(raw, list) or any(not isinstance(r, str) or not _SIZE_RULE.match(r) for r in raw):
        raise ValueError("excluded_sizes: нужен список правил вида «бренд|радиус» или «*|радиус»")
    return frozenset(raw)


_PACK4_RULE = re.compile(r"^([^|]+)\|(\*|summer|winter|all)\|(\*|[0-9]{1,2})\Z")


def _pack4_rules(raw) -> frozenset:
    raw = [] if raw is None else raw
    if not isinstance(raw, list) or any(not isinstance(r, str) or not _PACK4_RULE.match(r) for r in raw):
        raise ValueError("pack4_rules: нужен список правил вида «бренд|сезон|радиус» (вместо любого значения можно *)")
    return frozenset(raw)


def _overrides(raw) -> dict:
    raw = raw or {}
    if not isinstance(raw, dict) or any(v not in VALID for v in raw.values()):
        raise ValueError("overrides: допустимы только значения on и off")
    return {str(k): v for k, v in raw.items()}


def load_settings(path) -> Settings:
    return settings_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _finite(name: str, value) -> None:
    if not math.isfinite(float(value)):
        raise ValueError("%s must be a finite number" % name)


def _validate(s: Settings, enforce_pct_limit: bool = True) -> None:
    for name in ("price_jump_limit", "guard_fraction", "pct_limit"):
        _finite(name, getattr(s, name))
    ranges = (("min_stock", 1, 100000), ("hysteresis", 0, 100000), ("min_diameter", 1, 40), ("portion_limit", 0, 100000),
              ("stale_days", 0, 365), ("guard_min", 0, 100000), ("guard_fraction", 0, 1), ("date_end_days", 1, 365),
              ("date_end_renew_days", 0, 365), ("price_jump_limit", 0.0000001, 10000))
    for name, low, high in ranges:
        if not low <= getattr(s, name) <= high:
            raise ValueError("%s: допустимо от %s до %s" % (name, low, high))
    for pair, rule in s.pricing.items():
        _finite("percent for %s" % (pair,), rule.pct_small)
        _finite("percent for %s" % (pair,), rule.pct_large)
    for point, value in s.point_values.items():
        _finite("share of point %s" % point, value)
    for pair in s.enabled:
        if pair not in s.pricing:
            raise ValueError("enabled pair %s has no pricing rule" % (pair,))
    for pair, rule in s.pricing.items():
        for pct in (rule.pct_small, rule.pct_large):
            if enforce_pct_limit and abs(pct) > s.pct_limit:
                raise ValueError("percent %s for %s is outside limit +-%s" % (pct, pair, s.pct_limit))
    if s.point_mode not in ("percent", "count"):
        raise ValueError("point_mode must be percent or count")
    if s.point_mode == "percent" and abs(sum(s.point_values.values()) - 100) > 0.01:
        raise ValueError("point percentages must sum to 100")
    if s.point_mode == "count" and s.point_rest is not None and s.point_rest not in s.point_values:
        raise ValueError("point_rest must be one of the points")
