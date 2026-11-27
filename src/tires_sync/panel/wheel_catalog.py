"""Filtering for the wheel catalog; no effects on feed publication."""
from decimal import Decimal, InvalidOperation

PARAMETERS = {
    'width': ('width', 'Ширина'),
    'diameter': ('diameter', 'Диаметр'),
    'holes': ('holes', 'Количество отверстий'),
    'pcd': ('pcd', 'Расстояние PCD'),
    'offset': ('offset', 'Вылет ET'),
    'bore': ('center_bore', 'Диаметр ступицы'),
}


def number(value):
    try:
        value = str(value or '').strip().replace(',', '.')
        if not value or len(value) > 20:
            return None
        result = Decimal(value)
        return result if result.is_finite() and abs(result) <= 10000000 else None
    except (InvalidOperation, ValueError):
        return None


def choices(wheels, attribute):
    if attribute in {'brand', 'wheel_type'}:
        return sorted({getattr(w, attribute) for w in wheels if getattr(w, attribute)}, key=str.casefold)
    found = {}
    for wheel in wheels:
        value = getattr(wheel, attribute)
        numeric = number(value)
        if numeric is not None:
            found.setdefault(numeric, value)
    return [found[key] for key in sorted(found)]


def filtered(wheels, args, price_for):
    values = {key: args.get(key, '').strip()[:80] for key in (*PARAMETERS, 'type', 'brand', 'q', 'pf', 'pt', 'sn')}
    if 'sn' not in args:
        values['sn'] = '1'
    terms = values['q'].casefold().replace(',', '.').split()
    stock = number(values['sn'])
    low, high = number(values['pf']), number(values['pt'])
    rows = []
    for w in wheels:
        text = ' '.join(str(x) for x in (w.goods_id, w.name, w.brand, w.model, w.code, w.width, w.diameter,
                                         w.holes, w.pcd, w.offset, w.center_bore, w.wheel_type, w.color))
        if terms and not all(term in text.casefold().replace(',', '.') for term in terms):
            continue
        if values['brand'] and values['brand'].casefold() != w.brand.casefold():
            continue
        if values['type'] and values['type'] != w.wheel_type:
            continue
        if any(values[key] and (number(values[key]) is None or number(values[key]) != number(getattr(w, attr)))
               for key, (attr, _label) in PARAMETERS.items()):
            continue
        if values['sn'] and (stock is None or w.stock < stock):
            continue
        price = price_for(w)
        if values['pf'] and (low is None or price is None or price < low):
            continue
        if values['pt'] and (high is None or price is None or price > high):
            continue
        rows.append({'wheel': w, 'price': price})
    active = any(values[k] for k in (*PARAMETERS, 'type', 'brand', 'q', 'pf', 'pt')) or values['sn'] != '1'
    return rows, values, active
