"""Wheel-rim data from the separate WebMIM catalog unload (types=2).

Kept separate from tire offers: dimensions and publication rules differ.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class Wheel:
    goods_id: str
    name: str
    brand: str
    model: str
    code: str
    diameter: str
    width: str
    pcd: str
    holes: str
    offset: str
    center_bore: str
    wheel_type: str
    color: str
    stock: int
    price: Decimal
    image: str
    territory: str


@dataclass
class WheelCatalog:
    offers: dict[str, Wheel] = field(default_factory=dict)
    issues: dict[str, str] = field(default_factory=dict)


def parse_unload(data: bytes, territory: str) -> WheelCatalog:
    root = ET.fromstring(data)
    result = WheelCatalog()
    duplicates = set()
    for node in root.iter('COMMODITY'):
        def text(tag):
            return (node.findtext(tag) or '').strip()
        if text('TERRITORY_RN') != territory:
            continue
        gid = text('NNOMMODIF')
        if not gid:
            continue
        if gid in result.offers or gid in result.issues:
            duplicates.add(gid)
            continue
        try:
            stock = int(text('NREST'))
            price = Decimal(text('NPRICE_DELAYED').replace(',', '.'))
            if stock < 0 or not price.is_finite() or price < 0:
                raise ValueError('invalid stock or price')
        except (ValueError, InvalidOperation):
            result.issues[gid] = 'invalid_stock_or_price'
            continue
        result.offers[gid] = Wheel(
            gid, text('SMODIFNAME_ORIG') or text('SMODIFNAME'), text('SMARKA'), text('SMODEL'), text('SMNFCODE'),
            text('SDIAMETR'), text('SWIDTH'), text('SPCD') or text('SDPCD'), text('SHOLESQUANT'),
            text('SWHEELOFFSET'), text('SDIA'), text('STYPE'), text('SCOLOR'), stock, price, text('SPICTURE'), territory)
    for gid in duplicates:
        result.offers.pop(gid, None)
        result.issues[gid] = 'duplicate_in_territory'
    return result
