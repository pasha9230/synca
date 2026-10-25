"""Unit economics of the catalog: every position, every brand, the total (potential: by today's prices and stock)."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

from .. import economics, terms as terms_store
from ..models import Season
from ..pricing import final_price
from . import service


def season_of(offer) -> str:
    return offer.season.value if offer.season.value in ("winter", "summer", "all") else "all"


def positions(conn, effective, parsed, ref, day: str, params: Dict[str, float], filters: Optional[dict] = None) -> List[dict]:
    """One row per supplier position with its money for one tire and, where a second ad exists, for the set of four."""
    filters = filters or {}
    rules = terms_store.list_terms(conn)
    groups = service.catalog_groups(conn, effective, parsed, ref, "", "all", "brand", False)
    rows = []
    for group in groups:
        for item in group["items"]:
            offer = parsed.offers[item["goods_id"]]
            season = season_of(offer)
            if filters.get("season") and season != filters["season"]:
                continue
            if filters.get("diameter") and offer.diameter != filters["diameter"]:
                continue
            if filters.get("supplier") and service.supplier_of(offer) != filters["supplier"]:
                continue
            sale = item["price"]
            if sale is None:
                rule = effective.pricing.get((offer.brand_key, Season(season)))
                sale = final_price(offer.base_price, rule, offer.diameter) if rule and offer.base_price > 0 else None
            found = economics.pick_terms(rules, offer.brand_key, season, offer.diameter, day)
            row = {"goods_id": offer.goods_id, "brand": offer.brand_key, "name": item["name"], "model": item["model"], "size": item["size"],
                   "diameter": offer.diameter, "season": season, "kind": item["kind"], "stock": offer.stock, "state": item["state"],
                   "reason": item["reason"], "scode": item["scode"], "car_type": item["car_type"], "supplier_price": offer.base_price,
                   "sale": sale, "has_terms": found is not None, "unit": None, "set4": None}
            if sale is not None and offer.base_price > 0:
                row["unit"] = economics.unit(offer.base_price, sale, found, params["fee_pct"], params["promo_pct"])
                if effective.pack4_for(offer) and offer.stock >= 4:
                    row["set4"] = economics.unit(offer.base_price, sale, found, params["fee_pct"], params["promo_pct"], pieces=4)
            row["ads"] = (1 + (1 if row["set4"] else 0)) if item["state"] == "feed" else 0
            rows.append(row)
    return rows


def summarize(rows: List[dict]) -> dict:
    """Totals of a set of rows: only positions that are in the feed count (what is on sale now)."""
    feed = [r for r in rows if r["state"] == "feed" and r["unit"]]
    stock = sum(r["stock"] for r in feed)
    profit = sum(r["unit"]["margin"] * r["stock"] for r in feed)
    sale_total = sum(r["unit"]["sale"] * r["stock"] for r in feed)
    price_total = sum(r["supplier_price"] * r["stock"] for r in feed)
    return {
        "positions": len(rows), "in_feed": len(feed), "ads": sum(r["ads"] for r in rows), "stock": stock,
        "profit": round(profit, 2), "sale_total": round(sale_total, 2), "price_total": round(price_total, 2),
        "margin_pct_price": round(100.0 * profit / price_total, 2) if price_total else 0.0,
        "margin_pct_sale": round(100.0 * profit / sale_total, 2) if sale_total else 0.0,
        "no_terms": sum(1 for r in feed if not r["has_terms"]),
    }


def if_enabled(rows: List[dict]) -> dict:
    """What a brand would bring if all its positions that only wait for a switch (season or brand off) were published."""
    waiting = [r for r in rows if r["unit"] and r["scode"] in ("feed", "not_enabled", "manual_on")]
    price_total = sum(r["supplier_price"] * r["stock"] for r in waiting)
    profit = sum(r["unit"]["margin"] * r["stock"] for r in waiting)
    return {"positions": len(waiting), "profit": round(profit, 2),
            "margin_pct_price": round(100.0 * profit / price_total, 2) if price_total else 0.0}


def by_brand(rows: List[dict]) -> List[dict]:
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["brand"]].append(row)
    out = [dict(brand=brand, potential=if_enabled(items), **summarize(items)) for brand, items in grouped.items()]
    return sorted(out, key=lambda b: (not b["in_feed"], -b["profit"], b["brand"]))


def chart(series: List[dict], key: str, width: int = 640, height: int = 140, pad: int = 8) -> Optional[dict]:
    """A polyline for an inline SVG: the values of one key over the days (None while there are fewer than two days)."""
    if len(series) < 2:
        return None
    values = [float(point[key]) for point in series]
    low, high = min(values), max(values)
    step = (width - 2 * pad) / (len(values) - 1)
    if high == low:                                       # a flat line runs through the middle, not along the bottom
        points = ["%.1f,%.1f" % (pad + i * step, height / 2.0) for i in range(len(values))]
    else:
        points = ["%.1f,%.1f" % (pad + i * step, height - pad - (v - low) / (high - low) * (height - 2 * pad)) for i, v in enumerate(values)]
    return {"points": " ".join(points), "low": low, "high": high, "first": series[0]["day"], "last": series[-1]["day"],
            "width": width, "height": height, "now": values[-1]}
