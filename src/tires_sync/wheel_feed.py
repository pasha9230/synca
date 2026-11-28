"""Wheel feed primitives; no publishing or account configuration side effects.

The user-supplied Avito category template defines the field names. Category and
rim-type values must be configured explicitly; unconfigured brands do not
produce advertisements.
"""
from __future__ import annotations

from hashlib import sha256
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from .connections import PRIMARY_ACCOUNT
from .feed import clean_title
from .webmim import brand_key
from .wheel_pricing import price_for
from . import wheel_templates


def ad_id(goods_id: str, account_id: int, address_id: str) -> str:
    """Stable independent IDs for a wheel, account, and chosen address."""
    digest = sha256(('%s\0%s\0%s' % (account_id, address_id, goods_id)).encode()).hexdigest()[:24]
    return 'wheel_' + digest


def build_ads(wheels, rules, enabled_brands, *, account_id, address_id, phone, category, product_type, rim_types,
              photo_for=None, photo_prefix="", address_text="", overrides=None, publication_overrides=None, templates=None):
    """Build only ads with approved account photo URLs; supplier URLs are not published."""
    if account_id != PRIMARY_ACCOUNT or not str(address_id).strip():
        raise ValueError('Укажите аккаунт и адрес для дисков.')
    if not all((str(phone).strip(), str(category).strip(), str(product_type).strip())):
        raise ValueError('Для фида дисков нужны телефон и категории Авито.')
    ads, rejected = [], {}
    wheels = list(wheels)
    def identity(w):
        # Vendor duplicates of the same rim and color at one address must not
        # become two live listings with different supplier IDs.
        return tuple(' '.join(str(value or '').casefold().replace(',', '.').split()) for value in (
            w.brand, w.model, w.diameter, w.width, w.holes, w.pcd, w.offset, w.center_bore, w.wheel_type, w.color))
    def allowed(w):
        mode = (publication_overrides or {}).get('1:w-' + w.goods_id)
        return mode != 'off' and (mode == 'on' or (brand_key(w.brand) in enabled_brands and rules.get(brand_key(w.brand), {}).get('enabled') is True))
    groups = {}
    for w in wheels:
        key = brand_key(w.brand)
        if allowed(w):
            groups.setdefault(identity(w), []).append(w.goods_id)
    duplicates = {gid for ids in groups.values() if len(ids) > 1 for gid in ids}
    for w in wheels:
        key = brand_key(w.brand)
        if not allowed(w):
            continue
        if w.goods_id in duplicates:
            rejected[w.goods_id] = ['duplicate']
            continue
        price = price_for(w, rules, overrides)
        image_url = photo_for(w) if photo_for else ""
        parts = urlsplit(image_url or "")
        safe_photo = bool(photo_prefix and image_url.startswith(photo_prefix) and parts.scheme == "https"
                          and parts.hostname and not parts.username and not parts.password and not parts.query and not parts.fragment)
        rim_type = rim_types.get(w.wheel_type, '')
        missing = [k for k, v in {'brand': w.brand, 'name': w.name, 'diameter': w.diameter, 'width': w.width,
                                  'holes': w.holes, 'pcd': w.pcd, 'offset': w.offset, 'dia': w.center_bore,
                                  'rim_type': rim_type, 'price': price, 'image': image_url if safe_photo else ''}.items() if not v]
        if price is not None and price <= 0 and 'price' not in missing:
            missing.append('price')
        if w.stock <= 0 or missing:
            rejected[w.goods_id] = ['stock'] if w.stock <= 0 else missing
            continue
        ad = ET.Element('Ad')
        fields = {'Id': ad_id(w.goods_id, account_id, address_id), 'Category': category,
                  'GoodsType': 'Шины, диски и колёса', 'AdType': 'Товар приобретен на продажу',
                  'ProductType': product_type, 'Condition': 'Новое',
                  'Title': clean_title(wheel_templates.render(templates['title'], w, price, title=True) if templates and 'title' in templates else w.name),
                  'Description': wheel_templates.render(templates['description'], w, price) if templates and 'description' in templates else w.name,
                  'Price': str(price), 'RimBrand': w.brand, 'RimModel': w.model,
                  'RimDiameter': w.diameter, 'RimBolts': w.holes, 'RimBoltsDiameter': w.pcd,
                  'RimType': rim_type, 'RimDIA': w.center_bore, 'RimOffset': w.offset, 'RimWidth': w.width,
                  'Quantity': str(w.stock), 'ContactPhone': str(phone)}
        if address_text:
            fields['Address'] = str(address_text)
        else:
            fields['SellerAddressID'] = str(address_id)
        for tag, value in fields.items():
            if value:
                ET.SubElement(ad, tag).text = str(value)
        ET.SubElement(ET.SubElement(ad, 'Images'), 'Image', url=image_url)
        ads.append(ad)
    return ads, rejected


def assemble_mixed_feed(tire_feed: bytes, wheel_ads):
    """Prepare a mixed candidate in memory, without writing or publishing it.

    Duplicate IDs, unexpected category elements, and an invalid tire feed fail
    closed before any candidate can replace an existing feed.
    """
    try:
        root = ET.fromstring(tire_feed)
    except ET.ParseError as err:
        raise ValueError('Некорректный исходный фид шин.') from err
    if root.tag != 'Ads' or any(ad.tag != 'Ad' for ad in root):
        raise ValueError('Неожиданная структура исходного фида шин.')
    existing = [(ad.findtext('Id') or '').strip() for ad in root]
    if not all(existing) or len(set(existing)) != len(existing):
        raise ValueError('В исходном фиде отсутствуют или повторяются идентификаторы.')
    ids = set(existing)
    for ad in wheel_ads:
        if ad.tag != 'Ad':
            raise ValueError('Некорректное объявление диска.')
        aid = (ad.findtext('Id') or '').strip()
        if not aid.startswith('wheel_') or aid in ids:
            raise ValueError('Идентификатор диска отсутствует или повторяется.')
        ids.add(aid)
        root.append(ad)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def validate_ads(ads, *, template_tags, expected_ids, stock_by_id, allowed_addresses,
                 expected_phone, allowed_photo_prefix, foreign_ids=(), allowed_address_texts=()):
    """Check wheel candidates independently from tire-only validation rules.

    Expected identifiers and stocks are constructed from the trusted snapshot,
    not from the advertisement. An unknown id fails closed.
    """
    issues = []
    seen = set()
    required = ('Id', 'Category', 'GoodsType', 'AdType', 'ProductType', 'Condition', 'Title', 'Description',
                'Price', 'RimBrand', 'RimDiameter', 'RimBolts', 'RimBoltsDiameter', 'RimType',
                'RimDIA', 'RimOffset', 'RimWidth', 'Quantity', 'ContactPhone')
    for ad in ads:
        aid = (ad.findtext('Id') or '').strip()
        errors = []
        if not aid or aid in seen or aid in foreign_ids or aid not in expected_ids:
            errors.append('id')
        seen.add(aid)
        if any(child.tag not in template_tags for child in ad):
            errors.append('template')
        if any(not (ad.findtext(tag) or '').strip() for tag in required):
            errors.append('required')
        price, quantity = (ad.findtext('Price') or '').strip(), (ad.findtext('Quantity') or '').strip()
        if not price.isascii() or not price.isdigit() or int(price) <= 0:
            errors.append('price')
        if not quantity.isascii() or not quantity.isdigit() or int(quantity) <= 0 or int(quantity) > stock_by_id.get(aid, 0):
            errors.append('stock')
        address_id = (ad.findtext('SellerAddressID') or '').strip()
        address_text = (ad.findtext('Address') or '').strip()
        if bool(address_id) == bool(address_text) or (address_id and address_id not in allowed_addresses) or (address_text and address_text not in allowed_address_texts):
            errors.append('address')
        if (ad.findtext('ContactPhone') or '').strip() != expected_phone:
            errors.append('phone')
        image = ad.find('Images/Image')
        image_url = image.get('url', '') if image is not None else ''
        if not allowed_photo_prefix or not image_url.startswith(allowed_photo_prefix):
            errors.append('photo')
        if errors:
            issues.append((aid, errors))
    return issues


def checked_mixed_feed(tire_feed: bytes, wheel_ads, *, template_tags, expected_ids, stock_by_id,
                       allowed_addresses, expected_phone, allowed_photo_prefix, foreign_ids=(), allowed_address_texts=()):
    """Return mixed XML only after each wheel passes independently supplied facts."""
    errors = validate_ads(wheel_ads, template_tags=template_tags, expected_ids=expected_ids,
                          stock_by_id=stock_by_id, allowed_addresses=allowed_addresses,
                          expected_phone=expected_phone, allowed_photo_prefix=allowed_photo_prefix,
                          foreign_ids=foreign_ids, allowed_address_texts=allowed_address_texts)
    if errors:
        raise ValueError('Проверка дисков не пройдена: ' + '; '.join(
            '%s (%s)' % (aid, ', '.join(problems)) for aid, problems in errors[:10]))
    return assemble_mixed_feed(tire_feed, wheel_ads)
