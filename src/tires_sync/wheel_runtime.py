"""Stage wheel ads alongside a generated tire feed, without publishing it."""
from __future__ import annotations

import json
import hashlib
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from . import connections, store, wheel_feed, wheel_manifest, wheel_images
from .feed import write_atomic
from .settings_store import get_active
from .webmim_wheels import parse_unload
from .webmim import brand_key

# MIM's STYPE values; a missing or unknown mapping never produces an ad.
RIM_TYPES = {'Лит.': 'Литые', 'Штамп.': 'Штампованные'}
WHEEL_MAX_AGE = timedelta(hours=3)


def _config_digest(raw, min_stock):
    data = {'pricing': raw.get('wheel_pricing') or {}, 'accounts': raw.get('wheel_accounts') or {}, 'min_stock': min_stock}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _previous(conn, account, folder):
    path = folder / 'avito_feed.xml'
    if not path.exists():
        return [], {}, {}, {}
    data = path.read_bytes()
    root = ET.fromstring(data)
    wheels = [ad for ad in root.findall('Ad') if (ad.findtext('Id') or '').startswith('wheel_')]
    if not wheels:
        return [], {}, {}, {}
    info = wheel_manifest.load_info(conn, account['id'], data)
    facts = info['stocks'] if info else None
    if facts is None or set(facts) != {ad.findtext('Id') for ad in wheels}:
        raise ValueError('Не удалось проверить сведения об опубликованных дисках.')
    return wheels, facts, info.get('codes', {}), info.get('basis') or {}


def _snapshot(path, territory):
    if path is None:
        return None
    path = Path(path)
    try:
        metadata = json.loads(path.with_name(path.name + '.meta.json').read_text(encoding='utf-8'))
        stamp = datetime.fromisoformat(metadata['fetched_at'])
        if stamp.tzinfo is not None:
            return None
        age = datetime.utcnow() - stamp
        if age < -timedelta(minutes=5) or age > WHEEL_MAX_AGE:
            return None
        raw = path.read_bytes()
        result = parse_unload(raw, territory)
        result.snapshot_sha = hashlib.sha256(raw).hexdigest()
        if result.issues or not result.offers:
            return None
        return result
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError):
        return None


def append_candidate(conn, account, folder, eff, identity, wheel_path):
    """Modify next candidate only; a failure leaves the live feed untouched.

    Without a usable wheel snapshot, previous ads are retained unchanged using
    their SHA-bound manifest, so tire refresh cannot remove them accidentally.
    """
    if account['id'] != connections.PRIMARY_ACCOUNT:
        return {}, {}, ''
    settings_version, raw_settings = get_active(conn)
    rules = raw_settings.get('wheel_pricing', {})
    selected = raw_settings.get('wheel_accounts', {}).get(str(account['id']), [])
    enabled = {key for key, rule in rules.items() if rule.get('enabled') is True}
    previous, previous_stocks, previous_codes, previous_basis = _previous(conn, account, folder)
    if not selected or not enabled:
        return {}, {}, ''
    addresses = {a['address_id']: a for a in connections.list_addresses(conn, account['id'])}
    if any(address not in addresses for address in selected):
        if previous:
            raise ValueError('Для дисков выбран удалённый адрес основного аккаунта; прежний фид сохранён.')
        return {}, {}, 'диски не собраны: выбранный адрес основного аккаунта удалён' 
    _, prefix = connections.feed_style(conn, account['id'])
    phone = identity.get('phone') or eff.contact_phone
    if not prefix or not phone:
        if previous:
            raise ValueError('Для дисков нужен публичный адрес фотографий и телефон аккаунта; прежний фид сохранён.')
        return {}, {}, 'диски не собраны: нужен публичный адрес фотографий и телефон аккаунта' 
    mode = connections.get_address_mode(conn, account['id'])
    selected_ids = set(selected) if mode != 'text' else set()
    selected_texts = {addresses[a]['address_text'] for a in selected if addresses[a]['address_text']} if mode == 'text' else set()
    previous = [ad for ad in previous if brand_key(ad.findtext('RimBrand') or '') in enabled and
                (((ad.findtext('SellerAddressID') or '').strip() in selected_ids) or
                 ((ad.findtext('Address') or '').strip() in selected_texts))]
    previous_stocks = {ad.findtext('Id'): previous_stocks[ad.findtext('Id')] for ad in previous}
    snapshot = _snapshot(wheel_path, connections.supplier_territory(conn, 1, eff))
    source_path = str(Path(wheel_path).resolve()) if wheel_path is not None else ''
    try:
        source_sha = snapshot.snapshot_sha if snapshot is not None else hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
    except OSError:
        source_sha = 'missing'
    basis = {'path': source_path, 'sha': source_sha, 'settings_version': settings_version, 'config_sha': _config_digest(raw_settings, eff.min_stock)}
    stocks, codes, ads = {}, {}, []
    note = ''
    if snapshot is None:
        if previous and previous_basis.get('config_sha') != basis['config_sha']:
            raise ValueError('Настройки дисков изменились, а выгрузка МИМ недоступна: прежний фид сохранён до обновления выгрузки.')
        ads, stocks = previous, previous_stocks
        note = 'выгрузка дисков недоступна или устарела; прежние объявления сохранены'
        codes = {ad.findtext('Id'): previous_codes.get(ad.findtext('Id'), 'wheel:' + ad.findtext('Id')) for ad in ads}
    else:
        selected_rows = [o for o in snapshot.offers.values() if o.stock >= eff.min_stock]
        def safe_source(url):
            try:
                parts = urlsplit(url or '')
                return bool(parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password)
            except ValueError:
                return False
        wheel_images.remember(conn, {o.goods_id: o.image for o in selected_rows if re.fullmatch(r'[0-9]{1,40}', o.goods_id) and o.image})
        previous_ids = {ad.findtext('Id') for ad in previous}
        def address_for(w):
            # One listing per wheel avoids promising the same stock at every
            # selected point. Keep its old point while that point is selected.
            existing = [a for a in selected if wheel_feed.ad_id(w.goods_id, account['id'], a) in previous_ids]
            if existing:
                return sorted(existing)[0]
            return max(selected, key=lambda a: hashlib.sha256((w.goods_id + '\0' + a).encode()).digest())
        assignments = {w.goods_id: address_for(w) for w in selected_rows}
        for address_id in selected:
            address = addresses[address_id]
            text = address['address_text'] if connections.get_address_mode(conn, account['id']) == 'text' else ''
            if connections.get_address_mode(conn, account['id']) == 'text' and not text:
                raise ValueError('Для выбранной точки дисков не задан текстовый адрес.')
            created, rejected = wheel_feed.build_ads(
                [w for w in selected_rows if assignments[w.goods_id] == address_id], rules, enabled, account_id=account['id'], address_id=address_id,
                address_text=text, phone=phone, category='Запчасти и аксессуары', product_type='Диски', rim_types=RIM_TYPES,
                photo_prefix=prefix, photo_for=lambda w: prefix + 'w-' + w.goods_id + '.jpg' if safe_source(w.image) and re.fullmatch(r'[0-9]{1,40}', w.goods_id) else '')
            for w in selected_rows:
                aid = wheel_feed.ad_id(w.goods_id, account['id'], address_id)
                if any(ad.findtext('Id') == aid for ad in created):
                    stocks[aid] = w.stock
                    codes[aid] = 'wheel:' + w.goods_id
            # Missing data for an existing ad must not silently remove it.
            rejected_ids = {wheel_feed.ad_id(gid, account['id'], address_id): gid for gid in rejected}
            by_id = {w.goods_id: w for w in selected_rows}
            for old in previous:
                aid = old.findtext('Id')
                if aid in rejected_ids:
                    gid = rejected_ids[aid]
                    available = by_id[gid].stock
                    quantity = old.find('Quantity')
                    if quantity is None or not (quantity.text or '').isdigit():
                        raise ValueError('Нельзя проверить прежний остаток диска.')
                    quantity.text = str(min(int(quantity.text), available))
                    created.append(old)
                    stocks[aid] = available
                    codes[aid] = 'wheel:' + gid
            if rejected:
                note = 'часть дисков пропущена из-за неполных данных; прежние объявления сохранены'
            ads.extend(created)
    path = folder / 'next' / 'avito_feed.xml'
    if not ads:
        # A tire-only candidate still needs evidence for a deliberate stock
        # removal, including removal of the very last wheel.
        wheel_manifest.save(conn, account['id'], path.read_bytes(), {}, basis=basis, codes={})
        return {}, {}, note
    mixed = wheel_feed.assemble_mixed_feed(path.read_bytes(), ads)
    # An interrupted operation cannot publish: both pending SHA and wheel
    # manifest must match the exact candidate before the gate accepts it.
    write_atomic(path, mixed)
    store.rebind_pending(conn, account['id'], mixed)
    wheel_manifest.save(conn, account['id'], mixed, stocks, basis=basis, codes=codes)
    return stocks, codes, note


def check_before_publish(conn, account, feed_path, feed_root, removal_count=None):
    """Recheck wheel facts against the current account immediately before promotion."""
    from . import feed_check
    from .publish import PublishError
    from .settings_store import active_settings
    try:
        data = Path(feed_path).read_bytes()
        ads = ET.fromstring(data).findall('Ad')
    except (OSError, ET.ParseError) as err:
        raise PublishError('Не удалось проверить черновик фида перед публикацией.') from err
    checked_sha = hashlib.sha256(data).hexdigest()
    wheels = [ad for ad in ads if (ad.findtext('Id') or '').startswith('wheel_')]
    live_path = Path(feed_path).parent.parent / 'avito_feed.xml'
    try:
        live_data = live_path.read_bytes()
        live_wheels = [a for a in ET.fromstring(live_data).findall('Ad') if (a.findtext('Id') or '').startswith('wheel_')]
    except FileNotFoundError:
        live_wheels = []
    except (OSError, ET.ParseError) as err:
        raise PublishError('Не удалось проверить опубликованные диски перед заменой фида.') from err
    def groups(items):
        counts = {}
        for ad in items:
            key = (brand_key(ad.findtext('RimBrand') or ''), (ad.findtext('SellerAddressID') or ad.findtext('Address') or '').strip())
            counts[key] = counts.get(key, 0) + 1
        return counts
    before, after = groups(live_wheels), groups(wheels)
    reduced = {key for key, count in before.items() if after.get(key, 0) * 2 < count}
    if reduced:
        # Allow only changes explicitly confirmed in the settings, never a loss
        # caused by an incomplete supplier snapshot or an unexpected address.
        old_info = wheel_manifest.load_info(conn, account['id'], live_data)
        old_basis = old_info.get('basis') if old_info else None
        old_version = (old_basis or {}).get('settings_version')
        version, current = get_active(conn)
        row = conn.execute('SELECT json FROM settings_version WHERE id = ?', (old_version,)).fetchone() if old_version != version else None
        try:
            old = json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            old = {}
        old_rules, new_rules = old.get('wheel_pricing', {}), current.get('wheel_pricing', {})
        old_selected = set(old.get('wheel_accounts', {}).get(str(account['id']), []))
        new_selected = set(current.get('wheel_accounts', {}).get(str(account['id']), []))
        address_rows = connections.list_addresses(conn, account['id'])
        removed = old_selected - new_selected
        removed_values = set()
        if removed and old_selected != new_selected:
            for address in address_rows:
                if address['address_id'] in removed:
                    removed_values.add((address.get('address_text') or '').strip() if connections.get_address_mode(conn, account['id']) == 'text' else address['address_id'])
        candidate_info = wheel_manifest.load_info(conn, account['id'], data)
        candidate_basis = (candidate_info or {}).get('basis') or {}
        current_eff = connections.effective_settings(conn, account['id'], active_settings(conn))
        snapshot = _snapshot(candidate_basis.get('path'), connections.supplier_territory(conn, 1, current_eff))
        if (not snapshot or snapshot.snapshot_sha != candidate_basis.get('sha') or
                candidate_basis.get('config_sha') != _config_digest(current, current_eff.min_stock)):
            snapshot = None
        old_codes = (old_info or {}).get('codes') or {}
        candidate_ids = {ad.findtext('Id') for ad in wheels}
        def intentional(key):
            brand, address = key
            brand_disabled = bool(old_rules.get(brand, {}).get('enabled')) and not bool(new_rules.get(brand, {}).get('enabled'))
            if old and brand and address and (brand_disabled or address in removed_values):
                return True
            missing = [ad for ad in live_wheels if
                       (brand_key(ad.findtext('RimBrand') or ''), (ad.findtext('SellerAddressID') or ad.findtext('Address') or '').strip()) == key
                       and ad.findtext('Id') not in candidate_ids]
            if not snapshot or not missing:
                return False
            for ad in missing:
                code = old_codes.get(ad.findtext('Id'), '')
                offer = snapshot.offers.get(code[6:]) if code.startswith('wheel:') else None
                if offer is None or offer.stock >= current_eff.min_stock:
                    return False
            return True
        if any(not intentional(key) for key in reduced):
            raise PublishError('Число объявлений дисков отдельного бренда или точки сократилось более чем наполовину: публикация остановлена.')
        if removal_count is not None:
            removal_count.append(sum(1 for ad in live_wheels if ad.findtext('Id') not in candidate_ids and
                                     (brand_key(ad.findtext('RimBrand') or ''), (ad.findtext('SellerAddressID') or ad.findtext('Address') or '').strip()) in reduced))
    if not wheels:
        return checked_sha
    if account['id'] != connections.PRIMARY_ACCOUNT:
        raise PublishError('Диски разрешены только в основном аккаунте.')
    info = wheel_manifest.load_info(conn, account['id'], data)
    if info is None or not isinstance(info.get('basis'), dict):
        raise PublishError('Сведения о дисках не соответствуют фиду.')
    facts, basis = info['stocks'], info['basis']
    version, raw = get_active(conn)
    current_eff = connections.effective_settings(conn, account['id'], active_settings(conn))
    if basis.get('config_sha') != _config_digest(raw, current_eff.min_stock) or not basis.get('path'):
        raise PublishError('Настройки дисков изменились: фид нужно пересобрать.')
    try:
        current_sha = hashlib.sha256(Path(basis['path']).read_bytes()).hexdigest()
    except OSError:
        current_sha = 'missing'
    if current_sha != basis.get('sha'):
        raise PublishError('Выгрузка дисков изменилась: фид нужно пересобрать.')
    rules = raw.get('wheel_pricing', {})
    selected = set(raw.get('wheel_accounts', {}).get(str(account['id']), []))
    if any(not rules.get(brand_key(ad.findtext('RimBrand') or ''), {}).get('enabled') for ad in wheels):
        raise PublishError('Публикация бренда дисков выключена или настройка изменилась.')
    rows = [a for a in connections.list_addresses(conn, account['id']) if a['address_id'] in selected]
    if len(rows) != len(selected):
        raise PublishError('Выбранный адрес дисков больше не принадлежит аккаунту.')
    mode = connections.get_address_mode(conn, account['id'])
    address_ids = {a['address_id'] for a in rows} if mode != 'text' else set()
    address_texts = {a['address_text'] for a in rows if a['address_text']} if mode == 'text' else set()
    identity = connections.get_avito_identity(conn, account['id'])
    eff = connections.effective_settings(conn, account['id'], active_settings(conn))
    phone = identity.get('phone') or eff.contact_phone
    _, prefix = connections.feed_style(conn, account['id'])
    tags, problem = feed_check.read_template(Path(__file__).parent / 'data/avito-templates/diski-2026-11-26.xml')
    if problem:
        raise PublishError('Шаблон Авито для дисков недоступен.')
    foreign_ids = set()
    for other in connections.list_feed_accounts(conn):
        if other['id'] == account['id']:
            continue
        for path in (Path(feed_root) / other['feed_token'] / 'avito_feed.xml', Path(feed_root) / other['feed_token'] / 'next' / 'avito_feed.xml'):
            try:
                foreign_ids.update(a.findtext('Id') or '' for a in ET.parse(path).getroot().findall('Ad'))
            except (OSError, ET.ParseError):
                continue
    errors = wheel_feed.validate_ads(wheels, template_tags=tags, expected_ids=set(facts), stock_by_id=facts,
                                     allowed_addresses=address_ids, allowed_address_texts=address_texts,
                                     expected_phone=phone, allowed_photo_prefix=prefix, foreign_ids=foreign_ids)
    if errors or not store.pending_matches(conn, account['id'], data):
        raise PublishError('Проверка дисков или состояния фида не пройдена: публикация остановлена.')
    return checked_sha


def promote_checked(conn, account, folder, feed_root, *, promoter, **kwargs):
    """Promote exactly the checked bytes while wheel data and settings are locked."""
    from contextlib import ExitStack
    import fcntl
    import sqlite3
    from .publish import PublishError
    folder = Path(folder)
    candidate = folder / 'next' / 'avito_feed.xml'
    with ExitStack() as stack:
        # The timer and manual wheel refresh both use this same lock. Acquire it
        # without waiting so the independent wheel job cannot stall tire cycles.
        try:
            data = candidate.read_bytes()
            root = ET.fromstring(data)
            has_wheels = any((ad.findtext('Id') or '').startswith('wheel_') for ad in root.findall('Ad'))
            info = wheel_manifest.load_info(conn, account['id'], data) if account['id'] == connections.PRIMARY_ACCOUNT else None
            basis = info.get('basis') if info else None
            source = str((basis or {}).get('path') or '')
            if source:
                path = Path(source)
                name = path.name[:-4] + '.lock' if path.name.endswith('.wheels.xml') else path.name + '.lock'
                try:
                    lock = stack.enter_context(path.with_name(name).open('a'))
                except OSError as err:
                    raise PublishError('Не удалось заблокировать выгрузку дисков перед публикацией.') from err
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise PublishError('Выгрузка дисков обновляется: повторите публикацию.') from None
                stack.callback(fcntl.flock, lock, fcntl.LOCK_UN)
        except (OSError, ET.ParseError):
            pass  # The normal candidate checker reports a missing or invalid file.
        try:
            conn.execute('SAVEPOINT wheel_promotion')
            # Acquire SQLite's write reservation: no settings/address writer can
            # change facts until checked bytes have been atomically promoted.
            conn.execute("UPDATE app_state SET value=value WHERE key='wheel_promotion_lock'")
            removed = []
            checked_sha = check_before_publish(conn, account, candidate, feed_root, removal_count=removed)
            kwargs['expected_removals'] = kwargs.get('expected_removals', 0) + sum(removed)
            result = promoter(folder, expected_sha=checked_sha, **kwargs)
            conn.execute('RELEASE SAVEPOINT wheel_promotion')
            return result
        except Exception as err:
            try:
                conn.execute('ROLLBACK TO SAVEPOINT wheel_promotion')
                conn.execute('RELEASE SAVEPOINT wheel_promotion')
            except sqlite3.Error:
                pass
            if isinstance(err, sqlite3.Error):
                raise PublishError('Не удалось заблокировать настройки для проверки фида.') from err
            raise
