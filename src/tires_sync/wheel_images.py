"""Keep prior supplier image URLs for held wheel ads whose current image vanished."""
from __future__ import annotations

import json
import re

_PREFIX = 'wheel_image:'


def _key(goods_id):
    if not re.fullmatch(r'[0-9]{1,40}', str(goods_id or '')):
        raise ValueError('Некорректный идентификатор диска.')
    return _PREFIX + str(goods_id)


def sources(conn, goods_id):
    row = conn.execute('SELECT value FROM app_state WHERE key = ?', (_key(goods_id),)).fetchone()
    if row is None:
        return []
    try:
        values = json.loads(row[0])
    except (TypeError, ValueError):
        return []
    return [x for x in values if isinstance(x, str) and x.startswith('https://')][:2] if isinstance(values, list) else []


def remember(conn, images):
    """Retain current and preceding source; no remote fetch or publication."""
    for goods_id, image in images.items():
        if not isinstance(image, str) or not image.startswith('https://'):
            continue
        old = sources(conn, goods_id)
        values = list(dict.fromkeys([image] + old))[:2]
        conn.execute('INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)',
                     (_key(goods_id), json.dumps(values, ensure_ascii=False)))
    conn.commit()
