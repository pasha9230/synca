import json
import xml.etree.ElementTree as ET
from datetime import datetime
from test_wheel_catalog_filters import world
from tires_sync import connections, staging, wheel_archive
from tires_sync.fetcher import wheel_snapshot_path
from tires_sync.panel import service
from tires_sync.settings_store import get_active, confirm_draft
from tires_sync.webmim_wheels import parse_unload
from tires_sync.wheel_pricing import price_for
from tires_sync.wheel_feed import build_ads

NOW = '2026-11-27T10:00:00'


def setup_world(tmp_path):
    app, client, conn = world(tmp_path)
    assert connections.create_account(conn, 'Основной', NOW, 'owner') == 1
    path = wheel_snapshot_path(app.config['UNLOAD_PATH'])
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat()}))
    territory = connections.supplier_territory(conn, 1, __import__('tires_sync.settings_store', fromlist=['active_settings']).active_settings(conn))
    return app, client, conn, path, territory


def edit(client, value, gid='41', action='save'):
    return client.post('/catalog/wheels/own-price', data={'csrf': 'tok', 'g': gid, 'value': value, 'do': action, 'next': '/catalog?category=wheels&sn=0'})


def test_price_of_disappeared_wheel_is_saved_in_its_own_namespace(tmp_path):
    app, client, conn, path, territory = setup_world(tmp_path)
    old = parse_unload(path.read_bytes(), territory)
    wheel_archive.remember(conn, old.offers, NOW)
    root = ET.fromstring(path.read_bytes())
    for item in list(root):
        if item.findtext('NNOMMODIF') == '41':
            root.remove(item)
    path.write_bytes(ET.tostring(root))
    page = client.get('/catalog?category=wheels&sn=0').get_data(as_text=True)
    assert 'Silver rim' in page and 'Нет у поставщика' in page and 'Изменить цену' in page
    assert edit(client, '1899').status_code == 302
    changes = staging.summary(conn)['changes']
    assert changes['price_overrides']['1:w-41'] == {'price': 1899}
    assert '1:41' not in changes['price_overrides']
    active = get_active(conn)[1]
    assert '1:w-41' not in active.get('price_overrides', {})
    lines, risky = service._wheel_own_review(conn, active, changes)
    assert any('Silver rim' in line and '1 900' in line for line in lines)
    assert risky  # significant change must be confirmed
    confirm_draft(conn, staging.summary(conn)['draft_id'], NOW, 'owner', strong=True)
    active = get_active(conn)[1]
    assert price_for(old.offers['41'], active['wheel_pricing'], active['price_overrides']) == 1900
    rules = {'BRAND': {'pct_small': 10, 'pct_large': 10, 'enabled': True}}
    ads, rejected = build_ads([old.offers['41']], rules, {'BRAND'}, account_id=1, address_id='101', phone='123',
                              category='parts', product_type='wheels', rim_types={'Лит.': 'Литые'},
                              photo_for=lambda w: 'https://example.test/img.jpg', photo_prefix='https://example.test/',
                              overrides=active['price_overrides'])
    assert not rejected and ads[0].findtext('Price') == '1900'


def test_price_permission_and_unknown_id_are_enforced(tmp_path):
    from test_catalog_filters import as_user
    app, client, conn, path, territory = setup_world(tmp_path)
    viewer = as_user(app, conn, 'wheelviewer', 'viewer')
    page = viewer.get('/catalog?category=wheels').get_data(as_text=True)
    assert 'wheel_own_price' not in page and '/catalog/wheels/own-price' not in page
    assert edit(viewer, '9999').status_code == 403
    assert edit(client, '9999', gid='999999', action='save').status_code == 302
    assert not staging.summary(conn)['changes']


def test_wheel_price_is_independent_of_tire_and_respects_cost_warning(tmp_path):
    app, client, conn, path, territory = setup_world(tmp_path)
    assert edit(client, '500').status_code == 302
    changes = staging.summary(conn)['changes']
    lines, risky = service._wheel_own_review(conn, get_active(conn)[1], changes)
    assert risky and 'ниже закупки' in risky[0]
    assert price_for(wheel_archive.load(conn)['41'], {}, {'1:41': {'price': 7777}}) is None


def test_wheel_candidate_fingerprint_changes_with_manual_price(tmp_path):
    from tires_sync.wheel_runtime import _config_digest
    base = {'wheel_pricing': {}, 'wheel_accounts': {}, 'price_overrides': {}}
    changed = {**base, 'price_overrides': {'1:w-41': {'price': 2000}}}
    assert _config_digest(base, 1) != _config_digest(changed, 1)
    unrelated = {**base, 'price_overrides': {'1:41': {'price': 2000}}}
    assert _config_digest(base, 1) == _config_digest(unrelated, 1)


def test_unrelated_manual_wheel_price_does_not_stop_tire_updates_when_snapshot_is_missing(tmp_path):
    from test_webmim_wheels import _candidate_for_race_tests
    from helpers import catalog_xml, commodity_xml
    from test_cycle import REF, TODAY
    from tires_sync import cycle, publishing
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import create_draft
    conn, aid, root, path = _candidate_for_race_tests(tmp_path)
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    draft = create_draft(conn, {'price_overrides': {'1:w-999': {'price': 2000}}}, NOW)
    confirm_draft(conn, draft, NOW, 'owner', strong=True)
    path.unlink()
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NPRICE_DELAYED='5500')), parse_reference(REF), TODAY, root, wheel_path=path)
    assert result[0].ok, result[0].error
    result = publishing.publish_live_accounts(conn, root)
    assert result[0]['ok'], result[0]['message']


def test_held_wheel_uses_changed_price_if_photo_disappears(tmp_path):
    from test_webmim_wheels import _candidate_for_race_tests
    from helpers import catalog_xml, commodity_xml
    from test_cycle import REF, TODAY
    from tires_sync import cycle, publishing, connections
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import create_draft
    conn, aid, root, path = _candidate_for_race_tests(tmp_path)
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    draft = create_draft(conn, {'price_overrides': {'1:w-42': {'price': 1999}}}, NOW)
    confirm_draft(conn, draft, NOW, 'owner', strong=True)
    path.write_bytes(path.read_bytes().replace(b'<SPICTURE>https://example.test/image.jpg</SPICTURE>', b''))
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, root, wheel_path=path)
    assert result[0].ok, result[0].error
    account = connections.get_account(conn, aid)
    data = (root / account['feed_token'] / 'next' / 'avito_feed.xml').read_bytes()
    disk = next(ad for ad in ET.fromstring(data) if (ad.findtext('Id') or '').startswith('wheel_'))
    assert disk.findtext('Price') == '2000'
    result = publishing.publish_live_accounts(conn, root)
    assert result[0]['ok'], result[0]['message']


def test_only_missing_archive_entry_does_not_overwrite_latest_purchase(tmp_path):
    from dataclasses import replace
    from decimal import Decimal
    app, client, conn, path, territory = setup_world(tmp_path)
    wheel = parse_unload(path.read_bytes(), territory).offers['41']
    wheel_archive.remember(conn, {'41': replace(wheel, price=Decimal('2000'))}, NOW)
    wheel_archive.remember(conn, {'41': wheel}, NOW, only_missing=True)
    assert wheel_archive.load(conn)['41'].price == 2000


def test_draft_review_requires_confirmation_for_wheel_below_cost(tmp_path):
    app, client, conn, path, territory = setup_world(tmp_path)
    assert edit(client, '500').status_code == 302
    draft_id = staging.summary(conn)['draft_id']
    page = client.get('/draft/%s' % draft_id).get_data(as_text=True)
    assert 'Silver rim' in page and 'ниже закупки' in page
    refused = client.post('/draft/%s/confirm' % draft_id, data={'csrf': 'tok'}, follow_redirects=True).get_data(as_text=True)
    assert 'ниже закупки' in refused or 'отличается' in refused
    assert not get_active(conn)[1].get('price_overrides', {}).get('1:w-41')


def test_legacy_manifest_allows_existing_wheel_during_missing_snapshot_after_upgrade(tmp_path):
    import json, hashlib
    from test_webmim_wheels import _candidate_for_race_tests
    from helpers import catalog_xml, commodity_xml
    from test_cycle import REF, TODAY
    from tires_sync import cycle, publishing, connections, store
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import active_settings
    from tires_sync.wheel_runtime import _legacy_config_digest
    conn, aid, root, path = _candidate_for_race_tests(tmp_path)
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    account = connections.get_account(conn, aid)
    live = (root / account['feed_token'] / 'avito_feed.xml').read_bytes()
    key = 'wheel_manifest:1:' + hashlib.sha256(live).hexdigest()
    info = json.loads(store.get_state(conn, key))
    info['basis'].pop('wheel_ids', None)
    info['basis']['config_sha'] = _legacy_config_digest(get_active(conn)[1], connections.effective_settings(conn, aid, active_settings(conn)).min_stock)
    store.set_state(conn, key, json.dumps(info))
    path.unlink()
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NPRICE_DELAYED='5500')), parse_reference(REF), TODAY, root, wheel_path=path)
    assert result[0].ok, result[0].error
    result = publishing.publish_live_accounts(conn, root)
    assert result[0]['ok'], result[0]['message']


def test_review_uses_fresh_purchase_and_published_price(tmp_path):
    from dataclasses import replace
    from decimal import Decimal
    app, client, conn, path, territory = setup_world(tmp_path)
    wheel = parse_unload(path.read_bytes(), territory).offers['41']
    wheel_archive.remember(conn, {'41': wheel}, NOW)
    fresh = replace(wheel, price=Decimal('3000'))
    raw = get_active(conn)[1]
    changes = {'price_overrides': {'1:w-41': {'price': 2200}}}
    lines, risky = service._wheel_own_review(conn, raw, changes, {'41': fresh}, {'41': 1000})
    assert '1 000' in lines[0] and '2 200' in lines[0]
    assert risky and 'ниже закупки' in risky[0] and '3 000' in risky[0]
    assert 'отличается' in risky[0]


def test_legacy_manifest_stock_only_pass_removes_tire_without_blocking(tmp_path):
    import hashlib
    from datetime import date
    from test_stock_pass import NOW as T, REF, FIVE, AFTER
    from test_webmim_wheels import wheel
    from helpers import settings_dict
    from tires_sync import store, cycle, publishing
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import init_settings, active_settings
    from tires_sync.webmim import parse_unload as parse_tires
    from tires_sync.wheel_runtime import _legacy_config_digest
    conn = store.connect(tmp_path / 't.db')
    init_settings(conn, settings_dict(wheel_pricing={'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': True}}, wheel_accounts={'1': ['101']}), T)
    aid = connections.create_account(conn, 'Основной', T, 'owner')
    connections.set_live(conn, aid, True, T, 'owner')
    connections.save_addresses(conn, aid, 'percent', None, [{'name': 'Точка', 'address_id': '101', 'enabled': True, 'share': 100, 'address_text': 'СПб, Тестовая 1'}], T, 'owner', address_mode='text')
    store.set_state(conn, 'public_base', 'https://panel.example.test')
    path = tmp_path / 'wheels.xml'
    path.write_bytes(('<CATALOG>' + wheel(territory='T1').replace('</COMMODITY>', '<STYPE>Лит.</STYPE></COMMODITY>') + '</CATALOG>').encode())
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat()}))
    root, today, ref = tmp_path / 'feed', date(2026, 10, 24), parse_reference(REF)
    assert cycle.run_for_accounts(conn, FIVE, ref, today, root, wheel_path=path)[0].ok
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    account = connections.get_account(conn, aid)
    live = (root / account['feed_token'] / 'avito_feed.xml').read_bytes()
    key = 'wheel_manifest:1:' + hashlib.sha256(live).hexdigest()
    info = json.loads(store.get_state(conn, key))
    info['basis'].pop('wheel_ids', None)
    info['basis']['config_sha'] = _legacy_config_digest(get_active(conn)[1], connections.effective_settings(conn, aid, active_settings(conn)).min_stock)
    store.set_state(conn, key, json.dumps(info))
    result = cycle.stock_pass(conn, parse_tires(AFTER, 'T1'), ref, today, root)
    assert result and result[0].ok, result[0].error if result else 'нет результата'
    result = publishing.publish_live_accounts(conn, root)
    assert result[0]['ok'], result[0]['message']
