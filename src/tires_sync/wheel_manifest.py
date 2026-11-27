"""Bind wheel validation facts to exact feed content for safe rechecks."""
from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from . import store
from .connections import PRIMARY_ACCOUNT


def _key(account_id, feed_bytes):
    if account_id != PRIMARY_ACCOUNT:
        raise ValueError('Диски разрешены только в основном аккаунте.')
    return 'wheel_manifest:%d:%s' % (account_id, hashlib.sha256(feed_bytes).hexdigest())


def _ad_digest(ad):
    return hashlib.sha256(ET.tostring(ad, encoding='utf-8')).hexdigest()


def save(conn, account_id, feed_bytes, wheel_stocks, basis=None, codes=None):
    """Save facts keyed by feed SHA; reject missing, duplicated or foreign IDs."""
    root = ET.fromstring(feed_bytes)
    ads = root.findall('Ad')
    ids = [(ad.findtext('Id') or '').strip() for ad in ads]
    if len(set(ids)) != len(ids):
        raise ValueError('Повторяющийся идентификатор в фиде.')
    if any(not isinstance(k, str) or not k.startswith('wheel_') or isinstance(v, bool) or not isinstance(v, int) or v < 0
           for k, v in wheel_stocks.items()):
        raise ValueError('Некорректные сведения об остатках дисков.')
    wheels = {ad.findtext('Id'): _ad_digest(ad) for ad in ads if ad.findtext('Id') in wheel_stocks}
    if set(wheels) != set(wheel_stocks) or {i for i in ids if i.startswith('wheel_')} != set(wheel_stocks):
        raise ValueError('Данные дисков не соответствуют фиду.')
    codes = dict(codes or {k: 'wheel:' + k for k in wheel_stocks})
    if set(codes) != set(wheel_stocks) or any(not isinstance(v, str) or not v.startswith('wheel:') for v in codes.values()):
        raise ValueError('Коды дисков не соответствуют фиду.')
    value = {'stocks': wheel_stocks, 'digests': wheels, 'basis': basis, 'codes': codes}
    store.set_state(conn, _key(account_id, feed_bytes), json.dumps(value, ensure_ascii=False, sort_keys=True))


def load_info(conn, account_id, feed_bytes):
    """Read only facts for this exact XML and verify each wheel is unchanged."""
    raw = store.get_state(conn, _key(account_id, feed_bytes))
    if not raw:
        return None
    try:
        facts = json.loads(raw)
        root = ET.fromstring(feed_bytes)
        stocks, expected = facts['stocks'], facts['digests']
        ads = {ad.findtext('Id'): ad for ad in root.findall('Ad') if ad.findtext('Id') in expected}
        all_wheel_ids = {(ad.findtext('Id') or '') for ad in root.findall('Ad') if (ad.findtext('Id') or '').startswith('wheel_')}
        if set(ads) != set(expected) or set(stocks) != set(expected) or all_wheel_ids != set(expected):
            return None
        if any(_ad_digest(ad) != expected[key] for key, ad in ads.items()):
            return None
        if any(not k.startswith('wheel_') or isinstance(v, bool) or not isinstance(v, int) or v < 0 for k, v in stocks.items()):
            return None
        codes = facts.get('codes', {})
        if set(codes) != set(expected) or any(not isinstance(v, str) or not v.startswith('wheel:') for v in codes.values()):
            return None
        return {'stocks': stocks, 'basis': facts.get('basis'), 'codes': codes}
    except (ValueError, TypeError, KeyError, AttributeError, ET.ParseError):
        return None


def load(conn, account_id, feed_bytes):
    info = load_info(conn, account_id, feed_bytes)
    return info['stocks'] if info else None
