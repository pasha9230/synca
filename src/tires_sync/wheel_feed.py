"""Wheel feed primitives; no publishing or account configuration side effects.

The user-supplied Avito category template defines the field names. Category and
rim-type values must be configured explicitly; unconfigured brands do not
produce advertisements.
"""
from __future__ import annotations

from hashlib import sha256
import xml.etree.ElementTree as ET

from .connections import PRIMARY_ACCOUNT
from .feed import clean_title
from .webmim import brand_key
from .wheel_pricing import price_for


def ad_id(goods_id: str, account_id: int, address_id: str) -> str:
    """Stable independent IDs for a wheel, account, and chosen address."""
    digest = sha256(('%s\0%s\0%s' % (account_id, address_id, goods_id)).encode()).hexdigest()[:24]
    return 'wheel_' + digest


def build_ads(wheels, rules, enabled_brands, *, account_id, address_id, phone, category, product_type, rim_types):
    if account_id != PRIMARY_ACCOUNT or not str(address_id).strip():
        raise ValueError('Укажите аккаунт и адрес для дисков.')
    if not all((str(phone).strip(), str(category).strip(), str(product_type).strip())):
        raise ValueError('Для фида дисков нужны телефон и категории Авито.')
    ads, rejected = [], {}
    for w in wheels:
        key = brand_key(w.brand)
        if key not in enabled_brands or rules.get(key, {}).get('enabled') is not True:
            continue
        price = price_for(w, rules)
        rim_type = rim_types.get(w.wheel_type, '')
        missing = [k for k, v in {'brand': w.brand, 'name': w.name, 'diameter': w.diameter, 'width': w.width,
                                  'holes': w.holes, 'pcd': w.pcd, 'offset': w.offset, 'dia': w.center_bore,
                                  'rim_type': rim_type, 'price': price, 'image': w.image if w.image.startswith('https://') else ''}.items() if not v]
        if price is not None and price <= 0 and 'price' not in missing:
            missing.append('price')
        if w.stock <= 0 or missing:
            rejected[w.goods_id] = ['stock'] if w.stock <= 0 else missing
            continue
        ad = ET.Element('Ad')
        fields = {'Id': ad_id(w.goods_id, account_id, address_id), 'Category': category,
                  'GoodsType': 'Шины, диски и колёса', 'AdType': 'Товар приобретен на продажу',
                  'ProductType': product_type, 'Condition': 'Новое', 'Title': clean_title(w.name),
                  'Description': w.name, 'Price': str(price), 'RimBrand': w.brand, 'RimModel': w.model,
                  'RimDiameter': w.diameter, 'RimBolts': w.holes, 'RimBoltsDiameter': w.pcd,
                  'RimType': rim_type, 'RimDIA': w.center_bore, 'RimOffset': w.offset, 'RimWidth': w.width,
                  'Quantity': str(w.stock), 'SellerAddressID': str(address_id), 'ContactPhone': str(phone)}
        for tag, value in fields.items():
            if value:
                ET.SubElement(ad, tag).text = str(value)
        if w.image.startswith('https://'):
            ET.SubElement(ET.SubElement(ad, 'Images'), 'Image', url=w.image)
        ads.append(ad)
    return ads, rejected
