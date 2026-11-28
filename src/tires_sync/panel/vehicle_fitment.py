"""Local ANKUSI wheel reference, CC BY 4.0; see data/README.md."""
import gzip
import json
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def dataset():
    with gzip.open(Path(__file__).parent / 'data' / 'ankusi-fitment.json.gz', 'rt', encoding='utf-8') as stream:
        return json.load(stream)['data']


def years(row):
    match = re.fullmatch(r'(\d{4})(?:\s*[-–]\s*(\d{4}))?', row['year'])
    if not match:
        return ()
    start, end = int(match[1]), int(match[2] or match[1])
    return range(start, min(end, start + 100) + 1)


def selection(args):
    make = args.get('car_make', '')[:80]
    year = args.get('car_year', '')[:4]
    model = args.get('car_model', '')[:160]
    rows = dataset()
    makes = sorted({r['make'] for r in rows})
    candidates = [r for r in rows if r['make'] == make]
    available_years = sorted({y for r in candidates for y in years(r)}, reverse=True)
    if year.isdigit():
        candidates = [r for r in candidates if int(year) in years(r)]
    else:
        candidates = []
    models = sorted({r['model'] for r in candidates})
    matches = [r for r in candidates if r['model'] == model]
    # Distinct specifications are not guessed: the user must select a variant.
    variants = sorted({(r['chassis_code'], r['bolt_pattern'], r['center_bore_mm'], r['oe_wheel']) for r in matches})
    selected = args.get('car_variant', '')
    try:
        index = int(selected)
        variant = variants[index] if 0 <= index < len(variants) else None
    except ValueError:
        variant = None
    return {'make': make, 'year': year, 'model': model, 'selected': selected, 'makes': makes,
            'years': available_years, 'models': models, 'variants': variants, 'variant': variant}


def bolt_pattern(variant):
    if not variant:
        return None
    match = re.fullmatch(r'(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)', variant[1], re.I)
    return (match[1], match[2]) if match else None
