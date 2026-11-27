from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from http_fakes import ScriptedHttp
from tires_sync.webmim_client import WebmimClient
from tires_sync.webmim_wheels import parse_unload


def wheel(gid='42', territory='10', price='1234,50', stock='4'):
    return ('<COMMODITY><NNOMMODIF>' + gid + '</NNOMMODIF><TERRITORY_RN>' + territory +
            '</TERRITORY_RN><SMODIFNAME>Disk</SMODIFNAME><SMARKA>Brand</SMARKA>'
            '<SDIAMETR>16</SDIAMETR><SWIDTH>6,5</SWIDTH><SHOLESQUANT>5</SHOLESQUANT>'
            '<SPCD>114,3</SPCD><SWHEELOFFSET>40</SWHEELOFFSET><SDIA>66,1</SDIA><SPICTURE>https://example.test/image.jpg</SPICTURE>'
            '<NPRICE_DELAYED>' + price + '</NPRICE_DELAYED><NREST>' + stock + '</NREST></COMMODITY>')


def test_wheels_use_a_separate_mim_catalog():
    http = ScriptedHttp((200, '<CATALOG/>'))
    client = WebmimClient('https://example.test', mode='apikey', api_key='abc', http=http)
    client.fetch_wheels('10')
    assert parse_qs(urlparse(http.calls[0][1]).query)['types'] == ['2']


def test_wheel_attributes_and_territory():
    result = parse_unload(('<CATALOG>' + wheel() + wheel('43', '20') + '</CATALOG>').encode(), '10')
    rim = result.offers['42']
    assert len(result.offers) == 1 and rim.price == Decimal('1234.50') and rim.stock == 4
    assert (rim.width, rim.diameter, rim.holes, rim.pcd, rim.offset, rim.center_bore) == ('6,5', '16', '5', '114,3', '40', '66,1')


def test_bad_prices_and_duplicate_ids_do_not_enter_catalog():
    result = parse_unload(('<CATALOG>' + wheel() + wheel() + wheel('43', price='NaN') + '</CATALOG>').encode(), '10')
    assert not result.offers
    assert result.issues == {'42': 'duplicate_in_territory', '43': 'invalid_stock_or_price'}


def test_snapshot_guards_against_partial_replacement(tmp_path):
    import pytest
    from tires_sync.webmim_client import download_wheels, WebmimError
    from tires_sync.fetcher import wheel_snapshot_path

    target = wheel_snapshot_path(tmp_path / 'tires.xml')
    xml = ('<CATALOG>' + wheel() + wheel('43') + '</CATALOG>').encode()
    http = ScriptedHttp((200, xml), (200, b'<broken'))
    client = WebmimClient('https://example.test', mode='apikey', api_key='abc', http=http)
    assert download_wheels(client, '10', target, '2026-11-27')['items'] == 2
    with pytest.raises(WebmimError):
        download_wheels(client, '10', target, '2026-11-27')
    assert target.read_bytes() == xml
    assert not (tmp_path / 'tires.xml').exists()


def test_office_catalog_displays_wheels_without_leaking_into_tires(tmp_path):
    from test_catalog_filters import setup
    from tires_sync import connections
    from tires_sync.fetcher import wheel_snapshot_path
    from tires_sync.settings_store import active_settings

    app, owner, _, conn = setup(tmp_path)
    territory = connections.supplier_territory(conn, 1, active_settings(conn))
    target = wheel_snapshot_path(app.config['UNLOAD_PATH'])
    target.write_bytes(('<CATALOG>' + wheel(territory=territory) + '</CATALOG>').encode())
    page = owner.get('/catalog?category=wheels')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'Disk' in html and '114,3' in html and 'Диски' in html
    tire_page = owner.get('/catalog').get_data(as_text=True)
    assert 'Disk' not in tire_page and '/catalog?category=wheels' in tire_page


def test_client_cannot_open_office_wheel_catalog(tmp_path):
    from test_shop import make_shop
    _, client, _, _ = make_shop(tmp_path)
    assert client.get('/catalog?category=wheels').status_code == 403


def test_wheel_markup_is_staged_and_does_not_enable_publication(tmp_path):
    from test_catalog_filters import setup
    from panel_helpers import post
    from tires_sync import connections, staging
    from tires_sync.fetcher import wheel_snapshot_path
    from tires_sync.settings_store import active_settings
    from tires_sync.wheel_pricing import price_for

    app, owner, token, conn = setup(tmp_path)
    territory = connections.supplier_territory(conn, 1, active_settings(conn))
    xml = ('<CATALOG>' + wheel(territory=territory) + '</CATALOG>').encode()
    wheel_snapshot_path(app.config['UNLOAD_PATH']).write_bytes(xml)
    html = owner.get('/settings/brands').get_data(as_text=True)
    assert 'Диски: калькуляция и публикация' in html and 'Публиковать' in html
    response = post(owner, '/settings/pricing', token, scope='wheel', brand='BRAND', small='10', large='15')
    assert response.status_code == 302
    rules = staging.effective_raw(conn)['wheel_pricing']
    assert rules == {'BRAND': {'pct_small': 10.0, 'pct_large': 15.0, 'enabled': False}}
    assert price_for(parse_unload(xml, territory).offers['42'], rules) == 1400
    assert rules['BRAND']['enabled'] is False


def test_wheel_markup_uses_existing_confirmation_limit(tmp_path):
    import pytest
    from test_settings_store import fresh, T2
    from tires_sync import staging
    from tires_sync.settings_store import confirm_draft, PercentLimitError

    conn = fresh(tmp_path)
    staging.set_wheel_pricing(conn, 'BRAND', 500, 500, T2)
    draft_id = staging.summary(conn)['draft_id']
    with pytest.raises(PercentLimitError):
        confirm_draft(conn, draft_id, T2, 'owner')


def test_wheel_feed_is_opt_in_and_uses_template_fields():
    from pathlib import Path
    from tires_sync.wheel_feed import build_ads
    wheel_item = parse_unload(('<CATALOG>' + wheel() + '</CATALOG>').encode(), '10').offers['42']
    params = dict(account_id=1, address_id='25', phone='123', category='Запчасти и аксессуары',
                  product_type='Диски', rim_types={'': 'Литые'}, photo_for=lambda w: 'https://photos.example.test/a.jpg', photo_prefix='https://photos.example.test/')
    rules = {'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': True}}
    assert build_ads([wheel_item], rules, set(), **params) == ([], {})
    ads, rejected = build_ads([wheel_item], rules, {'BRAND'}, **params)
    assert not rejected and len(ads) == 1
    ad = ads[0]
    assert ad.findtext('RimBoltsDiameter') == '114,3' and ad.findtext('Price') == '1400'
    template = __import__('xml.etree.ElementTree', fromlist=['parse']).parse(str(
        Path(__file__).parents[1] / 'src/tires_sync/data/avito-templates/diski-2026-11-26.xml')).getroot()[0]
    assert {e.tag for e in ad} <= {e.tag for e in template}
    from tires_sync.wheel_feed import ad_id
    assert ad.findtext('Id') != ad_id('42', 2, '25')


def test_wheel_feed_rejects_nonpositive_prices():
    from tires_sync.wheel_feed import build_ads
    item = parse_unload(('<CATALOG>' + wheel() + '</CATALOG>').encode(), '10').offers['42']
    rules = {'BRAND': {'pct_small': -200, 'pct_large': 0, 'enabled': True}}
    ads, rejected = build_ads([item], rules, {'BRAND'}, account_id=1, address_id='2', phone='123',
                              category='parts', product_type='wheels', rim_types={'': 'type'}, photo_for=lambda w: 'https://photos.example.test/a.jpg', photo_prefix='https://photos.example.test/')
    assert ads == [] and rejected == {'42': ['price']}


def test_wheel_account_settings_staged_per_account_and_validate_inputs(tmp_path):
    import pytest
    from test_settings_store import fresh, T2
    from tires_sync import staging
    conn = fresh(tmp_path)
    staging.set_wheel_account(conn, 1, ['loc1', 'loc2'], T2)
    assert staging.effective_raw(conn)['wheel_accounts'] == {'1': ['loc1', 'loc2']}
    staging.set_wheel_account(conn, 1, [], T2)
    assert not staging.effective_raw(conn).get('wheel_accounts')
    with pytest.raises(ValueError):
        staging.set_wheel_account(conn, -1, ['loc1'], T2)


def test_wheel_account_selection_rejects_address_from_other_account(tmp_path):
    from test_catalog_filters import setup
    from panel_helpers import post
    from tires_sync import connections, staging
    app, owner, token, conn = setup(tmp_path)
    a = connections.create_account(conn, 'A', '2026-11-27', 'owner')
    b = connections.create_account(conn, 'B', '2026-11-27', 'owner')
    conn.execute("INSERT INTO avito_address (account_id,position,name,address_id,enabled,share,address_text) VALUES (?,0,'First','123',1,100,'')", (a,))
    conn.execute("INSERT INTO avito_address (account_id,position,name,address_id,enabled,share,address_text) VALUES (?,0,'Second','456',1,100,'')", (b,))
    conn.commit()
    assert post(owner, '/settings/pricing', token, scope='wheel_account', account=str(a), address='456').status_code == 302
    assert not staging.effective_raw(conn).get('wheel_accounts')
    assert post(owner, '/settings/pricing', token, scope='wheel_account', account=str(a), address='123').status_code == 302
    assert staging.effective_raw(conn)['wheel_accounts'] == {str(a): ['123']}
    assert post(owner, '/settings/pricing', token, scope='wheel_account', account=str(b), address='456').status_code == 302
    assert staging.effective_raw(conn)['wheel_accounts'] == {str(a): ['123']}


def test_extreme_price_is_rejected_without_losing_valid_rows():
    result = parse_unload(('<CATALOG>' + wheel() + wheel('43', price='1e999999') + '</CATALOG>').encode(), '10')
    assert list(result.offers) == ['42'] and result.issues['43'] == 'invalid_stock_or_price'


def test_wheel_feed_never_enables_brand_implicitly():
    from tires_sync.wheel_feed import build_ads
    item = parse_unload(('<CATALOG>' + wheel() + '</CATALOG>').encode(), '10').offers['42']
    params = dict(account_id=1, address_id='2', phone='123', category='parts', product_type='wheels', rim_types={'': 'type'}, photo_for=lambda w: 'https://photos.example.test/a.jpg', photo_prefix='https://photos.example.test/')
    for enabled in (None, False):
        rule = {'pct_small': 10, 'pct_large': 10}
        if enabled is not None:
            rule['enabled'] = enabled
        assert build_ads([item], {'BRAND': rule}, {'BRAND'}, **params) == ([], {})


def test_wrong_category_and_duplicates_preserve_existing_wheel_snapshot(tmp_path):
    import pytest
    from tires_sync.webmim_client import download_wheels, WebmimError
    target = tmp_path / 'wheels.xml'
    original = ('<CATALOG>' + wheel() + wheel('43') + '</CATALOG>').encode()
    wrong = b'<CATALOG><COMMODITY><NNOMMODIF>1</NNOMMODIF><TERRITORY_RN>10</TERRITORY_RN><NREST>4</NREST><NPRICE_DELAYED>5</NPRICE_DELAYED></COMMODITY></CATALOG>'
    duplicate = ('<CATALOG>' + wheel() + wheel() + '</CATALOG>').encode()
    client = WebmimClient('https://example.test', mode='apikey', api_key='abc', http=ScriptedHttp((200, original), (200, wrong), (200, duplicate)))
    download_wheels(client, '10', target, '2026-11-27')
    for _ in range(2):
        with pytest.raises(WebmimError):
            download_wheels(client, '10', target, '2026-11-27')
        assert target.read_bytes() == original


def test_wheel_settings_controls_are_in_page_content_not_title(tmp_path):
    from test_catalog_filters import setup
    from tires_sync import connections
    app, owner, _, conn = setup(tmp_path)
    connections.create_account(conn, 'Основной', '2026-11-27', 'owner')
    conn.commit()
    html = owner.get('/settings/brands').get_data(as_text=True)
    title = html.split('<title>', 1)[1].split('</title>', 1)[0]
    main = html.split('<main', 1)[1].split('</main>', 1)[0]
    assert '<section' not in title and 'Диски:' not in title
    assert html.count('id="pricing-wheels"') == 1
    assert 'name="scope" value="wheel_account"' in main


def test_wheel_settings_reject_stale_draft_edits(tmp_path):
    import pytest
    from test_settings_store import fresh, T2
    from tires_sync import staging
    conn = fresh(tmp_path)
    rev = staging.rev(staging.summary(conn)['changes'])
    staging.set_wheel_pricing(conn, 'BRAND', 10, 10, T2, expect=rev)
    with pytest.raises(staging.DraftChangedError):
        staging.set_wheel_account(conn, 1, ['loc'], T2, expect=rev)


def test_independent_wheel_fetch_command(tmp_path, monkeypatch):
    from test_cycle import fetch_setup
    from tires_sync import cli
    key_file = fetch_setup(tmp_path)
    body = ('<CATALOG>' + wheel(territory='103261489424') + '</CATALOG>').encode()
    http = ScriptedHttp((200, {'result': True, 'authKey': 'A'}), (200, '"%s"' % ('D' * 32)),
                        (200, {'result': True}), (200, body))
    monkeypatch.setattr(cli, '_http', lambda: http)
    code = cli.main(['fetch-wheels', '--db', str(tmp_path / 't.db'), '--unload', str(tmp_path / 'unload.xml'), '--key-file', str(key_file)])
    assert code == 0
    assert (tmp_path / 'unload.xml.wheels.xml').read_bytes() == body
    assert 'types=2' in http.calls[-1][1]


def test_wheel_reliability_identity_is_separate_from_tires(monkeypatch):
    from tires_sync import reliability
    seen = []
    def call(fn, **kw):
        seen.append(kw['service'])
        return fn()
    monkeypatch.setattr(reliability, 'call', call)
    http = ScriptedHttp((200, '<CATALOG/>'), (200, '<CATALOG/>'))
    WebmimClient('https://example.test', mode='apikey', api_key='abc', http=http).fetch_unload('10')
    WebmimClient('https://example.test', mode='apikey', api_key='abc', http=http,
                 service_id='webmim-wheels:example.test').fetch_wheels('10')
    assert seen == ['webmim:example.test', 'webmim-wheels:example.test']


def test_many_missing_ids_cannot_replace_snapshot(tmp_path):
    import pytest
    from tires_sync.webmim_client import download_wheels, WebmimError
    target = tmp_path / 'wheels.xml'
    original = ('<CATALOG>' + wheel() + '</CATALOG>').encode()
    broken = ('<CATALOG>' + wheel() + wheel(gid='') * 100 + '</CATALOG>').encode()
    client = WebmimClient('https://example.test', mode='apikey', api_key='abc', http=ScriptedHttp((200, broken), (200, original), (200, broken)))
    with pytest.raises(WebmimError):
        download_wheels(client, '10', target, '2026-11-27')
    assert not target.exists()
    download_wheels(client, '10', target, '2026-11-27')
    with pytest.raises(WebmimError):
        download_wheels(client, '10', target, '2026-11-27')
    assert target.read_bytes() == original


def test_wheel_feed_rejects_non_primary_account():
    import pytest
    from tires_sync.wheel_feed import build_ads
    with pytest.raises(ValueError):
        build_ads([], {}, set(), account_id=2, address_id='1', phone='123', category='parts', product_type='wheels', rim_types={})


def test_wheel_feed_refuses_unapproved_and_secret_photo_urls():
    from tires_sync.wheel_feed import build_ads
    item = parse_unload(('<CATALOG>' + wheel() + '</CATALOG>').encode(), '10').offers['42']
    rules = {'BRAND': {'pct_small': 10, 'pct_large': 10, 'enabled': True}}
    params = dict(account_id=1, address_id='2', phone='123', category='parts', product_type='wheels', rim_types={'': 'type'},
                  photo_prefix='https://photos.example.test/')
    for image in ('', 'https://other.test/a.jpg', 'https://photos.example.test/a.jpg?token=secret'):
        ads, rejected = build_ads([item], rules, {'BRAND'}, photo_for=lambda w: image, **params)
        assert ads == [] and rejected['42'] == ['image']


def test_mixed_feed_preserves_tires_and_rejects_duplicate_ids():
    import xml.etree.ElementTree as ET
    import pytest
    from tires_sync.wheel_feed import assemble_mixed_feed
    tire = b'<Ads formatVersion="3" target="Avito.ru"><Ad><Id>123</Id><Title>Tire</Title></Ad></Ads>'
    disk = ET.fromstring('<Ad><Id>wheel_abc</Id><Title>Wheel</Title></Ad>')
    combined = ET.fromstring(assemble_mixed_feed(tire, [disk]))
    assert [ad.findtext('Id') for ad in combined] == ['123', 'wheel_abc']
    assert combined[0].findtext('Title') == 'Tire'
    with pytest.raises(ValueError):
        assemble_mixed_feed(tire, [disk, disk])
    with pytest.raises(ValueError):
        assemble_mixed_feed(b'<Ads><Ad><Id>123</Id></Ad><Ad><Id>123</Id></Ad></Ads>', [])


def test_wheel_checks_fail_closed_on_unknown_stock_address_and_id():
    from pathlib import Path
    import xml.etree.ElementTree as ET
    from tires_sync.wheel_feed import build_ads, validate_ads
    item = parse_unload(('<CATALOG>' + wheel() + '</CATALOG>').encode(), '10').offers['42']
    rules = {'BRAND': {'pct_small': 10, 'pct_large': 10, 'enabled': True}}
    ads, _ = build_ads([item], rules, {'BRAND'}, account_id=1, address_id='2', phone='123', category='parts',
                        product_type='wheels', rim_types={'': 'type'}, photo_for=lambda w: 'https://example.test/a.jpg',
                        photo_prefix='https://example.test/')
    tags = {x.tag for x in ET.parse(str(Path(__file__).parents[1] / 'src/tires_sync/data/avito-templates/diski-2026-11-26.xml')).getroot()[0]}
    aid = ads[0].findtext('Id')
    facts = dict(template_tags=tags, expected_ids={aid}, stock_by_id={aid: 4}, allowed_addresses={'2'}, expected_phone='123',
                 allowed_photo_prefix='https://example.test/')
    assert validate_ads(ads, **facts) == []
    assert 'address' in validate_ads(ads, **dict(facts, allowed_addresses={'3'}))[0][1]
    assert 'stock' in validate_ads(ads, **dict(facts, stock_by_id={}))[0][1]
    assert 'id' in validate_ads(ads, **dict(facts, foreign_ids={aid}))[0][1]


def test_wheel_photo_is_separate_cached_and_only_for_primary_account(tmp_path):
    from test_account_photo import JPEG
    from test_catalog_filters import setup
    from tires_sync import connections
    from tires_sync.fetcher import wheel_snapshot_path
    from tires_sync.settings_store import active_settings
    calls = []
    app, client, _, conn = setup(tmp_path)
    app.config['PHOTO_FETCH'] = lambda url: (calls.append(url) or (JPEG, 'jpg'))
    first = connections.create_account(conn, 'Основной', '2026-11-27', 'owner')
    second = connections.create_account(conn, 'Другой', '2026-11-27', 'owner')
    conn.commit()
    territory = connections.supplier_territory(conn, 1, active_settings(conn))
    wheel_snapshot_path(app.config['UNLOAD_PATH']).write_bytes(('<CATALOG>' + wheel(territory=territory) + '</CATALOG>').encode())
    token = connections.get_account(conn, first)['feed_token']
    other = connections.get_account(conn, second)['feed_token']
    url = '/img/%s/w-42.jpg' % token
    assert client.get(url).data == JPEG
    assert client.get(url).data == JPEG and len(calls) == 1
    wheel_snapshot_path(app.config['UNLOAD_PATH']).unlink()
    assert client.get(url).data == JPEG
    assert client.get('/img/%s/w-42.jpg' % other).status_code == 404
    assert client.get('/img/%s/w-999.jpg' % token).status_code == 404


def test_mixed_gate_checks_wheel_without_tire_rules():
    from pathlib import Path
    import xml.etree.ElementTree as ET
    from tires_sync import feed_check
    from tires_sync.wheel_feed import build_ads
    item = parse_unload(('<CATALOG>' + wheel() + '</CATALOG>').encode(), '10').offers['42']
    rules = {'BRAND': {'pct_small': 10, 'pct_large': 10, 'enabled': True}}
    ads, _ = build_ads([item], rules, {'BRAND'}, account_id=1, address_id='2', phone='123', category='parts',
                        product_type='wheels', rim_types={'': 'type'}, photo_for=lambda w: 'https://example.test/a.jpg',
                        photo_prefix='https://example.test/')
    tags = frozenset(x.tag for x in ET.parse(str(Path(__file__).parents[1] / 'src/tires_sync/data/avito-templates/diski-2026-11-26.xml')).getroot()[0])
    aid = ads[0].findtext('Id')
    ctx = feed_check.Context(mode='generated', template_tags=frozenset(), phone='123', wheel_stocks={aid: 4},
                             wheel_template_tags=tags, wheel_addresses=frozenset({'2'}), wheel_photo_prefix='https://example.test/')
    assert feed_check.check_feed(ads, ctx) == []
    ctx.wheel_addresses = frozenset({'3'})
    assert any(f.level == 'critical' for f in feed_check.check_feed(ads, ctx))
    ctx.wheel_stocks = {}
    assert any(f.level == 'critical' for f in feed_check.check_feed(ads, ctx))


def test_checked_mixed_feed_wont_emit_unverified_wheels():
    import pytest
    import xml.etree.ElementTree as ET
    from tires_sync.wheel_feed import checked_mixed_feed
    tire = b'<Ads><Ad><Id>tire123</Id></Ad></Ads>'
    disk = ET.fromstring('<Ad><Id>wheel_bad</Id><Price>0</Price></Ad>')
    with pytest.raises(ValueError, match='Проверка дисков'):
        checked_mixed_feed(tire, [disk], template_tags=frozenset({'Id', 'Price'}), expected_ids={'wheel_bad'},
                           stock_by_id={'wheel_bad': 4}, allowed_addresses={'1'}, expected_phone='123',
                           allowed_photo_prefix='https://example.test/')


def test_wheel_ads_support_text_addresses_without_using_internal_id():
    from pathlib import Path
    import xml.etree.ElementTree as ET
    from tires_sync.wheel_feed import build_ads, validate_ads
    item = parse_unload(('<CATALOG>' + wheel() + '</CATALOG>').encode(), '10').offers['42']
    rules = {'BRAND': {'pct_small': 10, 'pct_large': 10, 'enabled': True}}
    ads, _ = build_ads([item], rules, {'BRAND'}, account_id=1, address_id='internal1', address_text='Санкт-Петербург, Тестовая 1',
                        phone='123', category='parts', product_type='wheels', rim_types={'': 'type'},
                        photo_for=lambda w: 'https://example.test/a.jpg', photo_prefix='https://example.test/')
    ad = ads[0]
    assert ad.findtext('Address') == 'Санкт-Петербург, Тестовая 1' and ad.find('SellerAddressID') is None
    tags = frozenset(x.tag for x in ET.parse(str(Path(__file__).parents[1] / 'src/tires_sync/data/avito-templates/diski-2026-11-26.xml')).getroot()[0])
    aid = ad.findtext('Id')
    facts = dict(template_tags=tags, expected_ids={aid}, stock_by_id={aid: 4}, allowed_addresses=set(), expected_phone='123',
                 allowed_photo_prefix='https://example.test/')
    assert validate_ads(ads, allowed_address_texts={'Санкт-Петербург, Тестовая 1'}, **facts) == []
    assert 'address' in validate_ads(ads, allowed_address_texts={'Другой адрес'}, **facts)[0][1]


def test_wheel_manifest_is_bound_to_exact_feed_and_primary_account(tmp_path):
    import pytest
    from tires_sync import store, wheel_manifest
    conn = store.connect(tmp_path / 'manifest.db')
    original = b'<Ads><Ad><Id>tire</Id></Ad><Ad><Id>wheel_a</Id><Price>1000</Price></Ad></Ads>'
    wheel_manifest.save(conn, 1, original, {'wheel_a': 4})
    assert wheel_manifest.load(conn, 1, original) == {'wheel_a': 4}
    assert wheel_manifest.load(conn, 1, original.replace(b'1000', b'1001')) is None
    with pytest.raises(ValueError):
        wheel_manifest.save(conn, 2, original, {'wheel_a': 4})
    with pytest.raises(ValueError):
        wheel_manifest.save(conn, 1, original, {'wheel_missing': 4})
    with pytest.raises(ValueError):
        wheel_manifest.save(conn, 1, original, {'tire': 4})


def test_manifest_rejects_unaccounted_wheel_ad(tmp_path):
    import pytest
    from tires_sync import store, wheel_manifest
    conn = store.connect(tmp_path / 'manifest2.db')
    feed = b'<Ads><Ad><Id>wheel_a</Id></Ad><Ad><Id>wheel_b</Id></Ad></Ads>'
    with pytest.raises(ValueError):
        wheel_manifest.save(conn, 1, feed, {'wheel_a': 4})


def test_generated_feed_combines_tires_and_wheels_only_for_primary_account(tmp_path):
    import json
    from datetime import datetime
    import xml.etree.ElementTree as ET
    from helpers import catalog_xml, commodity_xml
    from test_cycle import NOW, TODAY, REF, prepared
    from tires_sync import connections, cycle, store
    from tires_sync.catalog import parse_reference

    conn = prepared(tmp_path, wheel_pricing={'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': True}},
                    wheel_accounts={'1': ['101']})
    main = connections.create_account(conn, 'Основной', NOW, 'owner')
    other = connections.create_account(conn, 'Другой', NOW, 'owner')
    connections.save_addresses(conn, main, 'percent', None,
                               [{'name': 'Точка', 'address_id': '101', 'enabled': True, 'share': 100,
                                 'address_text': 'Санкт-Петербург, Тестовая 1'}], NOW, 'owner', address_mode='text')
    store.set_state(conn, 'public_base', 'https://panel.example.test')
    path = tmp_path / 'wheels.xml'
    path.write_bytes(('<CATALOG>' + wheel(territory='T1').replace('</COMMODITY>', '<STYPE>Лит.</STYPE></COMMODITY>') + '</CATALOG>').encode())
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat(timespec='seconds')}))
    results = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, tmp_path / 'feed', wheel_path=path)
    assert [(r.account_id, r.ok, r.error) for r in results] == [(main, True, ''), (other, True, '')]
    a = connections.get_account(conn, main)
    b = connections.get_account(conn, other)
    first = ET.parse(tmp_path / 'feed' / a['feed_token'] / 'next' / 'avito_feed.xml').getroot().findall('Ad')
    second = ET.parse(tmp_path / 'feed' / b['feed_token'] / 'next' / 'avito_feed.xml').getroot().findall('Ad')
    assert any(ad.findtext('Id').startswith('wheel_') for ad in first)
    assert not any(ad.findtext('Id').startswith('wheel_') for ad in second)
    disk = next(ad for ad in first if ad.findtext('Id').startswith('wheel_'))
    assert disk.findtext('Address') == 'Санкт-Петербург, Тестовая 1'
    assert disk.findtext('RimBoltsDiameter') == '114,3'
    assert store.pending_matches(conn, main, (tmp_path / 'feed' / a['feed_token'] / 'next' / 'avito_feed.xml').read_bytes())


def test_missing_wheel_snapshot_preserves_published_wheel_while_tires_update(tmp_path):
    import json
    from datetime import datetime
    import xml.etree.ElementTree as ET
    from helpers import catalog_xml, commodity_xml
    from test_cycle import NOW, TODAY, REF, prepared
    from tires_sync import connections, cycle, store, publishing
    from tires_sync.catalog import parse_reference

    conn = prepared(tmp_path, wheel_pricing={'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': True}},
                    wheel_accounts={'1': ['101']})
    account_id = connections.create_account(conn, 'Основной', NOW, 'owner')
    connections.set_live(conn, account_id, True, NOW, 'owner')
    connections.save_addresses(conn, account_id, 'percent', None,
                               [{'name': 'Точка', 'address_id': '101', 'enabled': True, 'share': 100,
                                 'address_text': 'Санкт-Петербург, Тестовая 1'}], NOW, 'owner', address_mode='text')
    store.set_state(conn, 'public_base', 'https://panel.example.test')
    path = tmp_path / 'wheels.xml'
    path.write_bytes(('<CATALOG>' + wheel(territory='T1').replace('</COMMODITY>', '<STYPE>Лит.</STYPE></COMMODITY>') + '</CATALOG>').encode())
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat(timespec='seconds')}))
    feed_root = tmp_path / 'feed'
    ref = parse_reference(REF)
    first = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), ref, TODAY, feed_root, wheel_path=path)
    assert first[0].ok, first[0].error
    assert publishing.publish_live_accounts(conn, feed_root)[0]['ok']
    account = connections.get_account(conn, account_id)
    public = feed_root / account['feed_token'] / 'avito_feed.xml'
    original = next(a for a in ET.fromstring(public.read_bytes()) if (a.findtext('Id') or '').startswith('wheel_'))
    path.unlink()
    second = cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NPRICE_DELAYED='5500')), ref, TODAY, feed_root, wheel_path=path)
    assert second[0].ok, second[0].error
    assert 'прежние объявления сохранены' in second[0].error
    assert publishing.publish_live_accounts(conn, feed_root)[0]['ok']
    saved = next(a for a in ET.fromstring(public.read_bytes()) if (a.findtext('Id') or '').startswith('wheel_'))
    assert ET.tostring(original) == ET.tostring(saved)


def test_stock_only_pass_keeps_wheels_when_tires_disappear(tmp_path):
    import json
    import xml.etree.ElementTree as ET
    from datetime import datetime, date
    from test_stock_pass import NOW, REF, FIVE, AFTER
    from tires_sync import connections, cycle, store, publishing
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import init_settings
    from tires_sync.webmim import parse_unload as parse_tires
    from helpers import settings_dict

    conn = store.connect(tmp_path / 't.db')
    init_settings(conn, settings_dict(wheel_pricing={'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': True}},
                                      wheel_accounts={'1': ['101']}), NOW)
    account_id = connections.create_account(conn, 'Основной', NOW, 'owner')
    connections.set_live(conn, account_id, True, NOW, 'owner')
    connections.save_addresses(conn, account_id, 'percent', None,
                               [{'name': 'Точка', 'address_id': '101', 'enabled': True, 'share': 100,
                                 'address_text': 'Санкт-Петербург, Тестовая 1'}], NOW, 'owner', address_mode='text')
    store.set_state(conn, 'public_base', 'https://panel.example.test')
    path = tmp_path / 'wheels.xml'
    path.write_bytes(('<CATALOG>' + wheel(territory='T1').replace('</COMMODITY>', '<STYPE>Лит.</STYPE></COMMODITY>') + '</CATALOG>').encode())
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat(timespec='seconds')}))
    root, today, ref = tmp_path / 'feed', date(2026, 10, 24), parse_reference(REF)
    initial = cycle.run_for_accounts(conn, FIVE, ref, today, root, wheel_path=path)
    assert initial[0].ok, initial[0].error
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    account = connections.get_account(conn, account_id)
    public = root / account['feed_token'] / 'avito_feed.xml'
    original = next(a for a in ET.fromstring(public.read_bytes()) if (a.findtext('Id') or '').startswith('wheel_'))
    result = cycle.stock_pass(conn, parse_tires(AFTER, 'T1'), ref, today, root)
    assert result and result[0].ok, result[0].error if result else 'нет результата'
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    saved = next(a for a in ET.fromstring(public.read_bytes()) if (a.findtext('Id') or '').startswith('wheel_'))
    assert ET.tostring(original) == ET.tostring(saved)


def test_stale_snapshot_is_not_used_for_new_wheel_publication(tmp_path):
    import json
    from datetime import datetime, timedelta
    from tires_sync.wheel_runtime import _snapshot
    path = tmp_path / 'wheels.xml'
    path.write_bytes(('<CATALOG>' + wheel(territory='T1') + '</CATALOG>').encode())
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': (datetime.utcnow() - timedelta(hours=4)).isoformat()}))
    assert _snapshot(path, 'T1') is None
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat()}))
    assert _snapshot(path, 'T1') is not None


def test_disabled_wheel_brand_never_enters_live_tire_feed(tmp_path):
    from datetime import datetime
    import json
    import xml.etree.ElementTree as ET
    from helpers import catalog_xml, commodity_xml
    from test_cycle import NOW, TODAY, REF, prepared
    from tires_sync import connections, cycle, store
    from tires_sync.catalog import parse_reference
    conn = prepared(tmp_path, wheel_pricing={'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': False}},
                    wheel_accounts={'1': ['101']})
    account_id = connections.create_account(conn, 'Основной', NOW, 'owner')
    store.set_state(conn, 'public_base', 'https://panel.example.test')
    path = tmp_path / 'wheels.xml'
    path.write_bytes(('<CATALOG>' + wheel(territory='T1').replace('</COMMODITY>', '<STYPE>Лит.</STYPE></COMMODITY>') + '</CATALOG>').encode())
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat()}))
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, tmp_path / 'feed', wheel_path=path)
    assert result[0].ok, result[0].error
    account = connections.get_account(conn, account_id)
    ads = ET.parse(tmp_path / 'feed' / account['feed_token'] / 'next' / 'avito_feed.xml').getroot()
    assert not any((ad.findtext('Id') or '').startswith('wheel_') for ad in ads)


def test_publish_rechecks_wheel_address_after_draft_was_built(tmp_path):
    import json
    from datetime import datetime
    from helpers import catalog_xml, commodity_xml
    from test_cycle import NOW, TODAY, REF, prepared
    from tires_sync import connections, cycle, store, publishing
    from tires_sync.catalog import parse_reference
    conn = prepared(tmp_path, wheel_pricing={'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': True}},
                    wheel_accounts={'1': ['101']})
    aid = connections.create_account(conn, 'Основной', NOW, 'owner')
    connections.set_live(conn, aid, True, NOW, 'owner')
    connections.save_addresses(conn, aid, 'percent', None,
                               [{'name': 'Точка', 'address_id': '101', 'enabled': True, 'share': 100,
                                 'address_text': 'Санкт-Петербург, Тестовая 1'}], NOW, 'owner', address_mode='text')
    store.set_state(conn, 'public_base', 'https://panel.example.test')
    path = tmp_path / 'wheels.xml'
    path.write_bytes(('<CATALOG>' + wheel(territory='T1').replace('</COMMODITY>', '<STYPE>Лит.</STYPE></COMMODITY>') + '</CATALOG>').encode())
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat()}))
    root = tmp_path / 'feed'
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, root, wheel_path=path)
    assert result[0].ok, result[0].error
    connections.save_addresses(conn, aid, 'percent', None,
                               [{'name': 'Другая точка', 'address_id': '102', 'enabled': True, 'share': 100,
                                 'address_text': 'Санкт-Петербург, Другая 2'}], NOW, 'owner', address_mode='text')
    published = publishing.publish_live_accounts(conn, root)
    assert not published[0]['ok'] and 'адрес' in published[0]['message']
    account = connections.get_account(conn, aid)
    assert not (root / account['feed_token'] / 'avito_feed.xml').exists()


def test_wheel_fetch_lock_is_separate_from_tire_lock(tmp_path):
    import fcntl
    import pytest
    from tires_sync.fetcher import fetch_wheels_for_connection
    from tires_sync.webmim_client import WebmimError
    dest = tmp_path / 'unload.xml'
    lock = dest.with_name(dest.name + '.wheels.lock')
    with lock.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(WebmimError, match='уже обновляется'):
            fetch_wheels_for_connection(None, None, dest, '2026-11-27')
    assert not (tmp_path / '.fetch.lock').exists()


def test_held_wheel_quantity_never_exceeds_new_supplier_stock(tmp_path):
    import json
    import xml.etree.ElementTree as ET
    from datetime import datetime
    from helpers import catalog_xml, commodity_xml
    from test_cycle import NOW, TODAY, REF, prepared
    from tires_sync import connections, cycle, store, publishing
    from tires_sync.catalog import parse_reference
    conn = prepared(tmp_path, wheel_pricing={'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': True}},
                    wheel_accounts={'1': ['101']})
    aid = connections.create_account(conn, 'Основной', NOW, 'owner')
    connections.set_live(conn, aid, True, NOW, 'owner')
    connections.save_addresses(conn, aid, 'percent', None,
                               [{'name': 'Точка', 'address_id': '101', 'enabled': True, 'share': 100,
                                 'address_text': 'Санкт-Петербург, Тестовая 1'}], NOW, 'owner', address_mode='text')
    store.set_state(conn, 'public_base', 'https://panel.example.test')
    path = tmp_path / 'wheels.xml'
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat()}))
    path.write_bytes(('<CATALOG>' + wheel(territory='T1', stock='100').replace('</COMMODITY>', '<STYPE>Лит.</STYPE></COMMODITY>') + '</CATALOG>').encode())
    root, ref = tmp_path / 'feed', parse_reference(REF)
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), ref, TODAY, root, wheel_path=path)
    assert result[0].ok, result[0].error
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    path.write_bytes(('<CATALOG>' + wheel(territory='T1', stock='4') + '</CATALOG>').encode())
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), ref, TODAY, root, wheel_path=path)
    assert result[0].ok, result[0].error
    account = connections.get_account(conn, aid)
    candidate = root / account['feed_token'] / 'next' / 'avito_feed.xml'
    disk = next(a for a in ET.fromstring(candidate.read_bytes()) if (a.findtext('Id') or '').startswith('wheel_'))
    assert disk.findtext('Quantity') == '4'
    assert publishing.publish_live_accounts(conn, root)[0]['ok']


def test_mass_wheel_removal_is_guarded_even_in_large_tire_feed(tmp_path):
    import pytest
    from tires_sync import store, connections
    from tires_sync.publish import PublishError
    from tires_sync.wheel_runtime import check_before_publish
    from test_settings_store import fresh, T2
    conn = fresh(tmp_path)
    aid = connections.create_account(conn, 'Основной', T2, 'owner')
    account = connections.get_account(conn, aid)
    folder = tmp_path / account['feed_token']
    (folder / 'next').mkdir(parents=True)
    tires = ''.join('<Ad><Id>t%d</Id></Ad>' % i for i in range(100))
    wheels = ''.join('<Ad><Id>wheel_%d</Id></Ad>' % i for i in range(5))
    (folder / 'avito_feed.xml').write_text('<Ads>' + tires + wheels + '</Ads>')
    (folder / 'next' / 'avito_feed.xml').write_text('<Ads>' + tires + '</Ads>')
    with pytest.raises(PublishError, match='сократилось'):
        check_before_publish(conn, account, folder / 'next' / 'avito_feed.xml', tmp_path)


def test_held_wheel_image_is_available_after_source_disappears_and_cache_is_empty(tmp_path):
    from test_account_photo import JPEG
    from test_catalog_filters import setup
    from tires_sync import connections, wheel_images
    app, client, _, conn = setup(tmp_path)
    app.config['PHOTO_FETCH'] = lambda url: (JPEG, 'jpg')
    aid = connections.create_account(conn, 'Основной', '2026-11-27', 'owner')
    conn.commit()
    wheel_images.remember(conn, {'42': 'https://example.test/old.jpg'})
    token = connections.get_account(conn, aid)['feed_token']
    assert client.get('/img/%s/w-42.jpg' % token).data == JPEG


def test_missing_entire_wheel_brand_is_guarded_with_other_wheels_unchanged(tmp_path):
    import pytest
    from tires_sync import connections
    from tires_sync.publish import PublishError
    from tires_sync.wheel_runtime import check_before_publish
    from test_settings_store import fresh, T2
    conn = fresh(tmp_path)
    aid = connections.create_account(conn, 'Основной', T2, 'owner')
    account = connections.get_account(conn, aid)
    folder = tmp_path / account['feed_token']
    (folder / 'next').mkdir(parents=True)
    make = lambda prefix, brand: ''.join('<Ad><Id>wheel_%s%d</Id><RimBrand>%s</RimBrand><Address>A</Address></Ad>' % (prefix, i, brand) for i in range(10))
    tires = '<Ad><Id>tire</Id></Ad>'
    keep = make('keep', 'Brand A')
    (folder / 'avito_feed.xml').write_text('<Ads>' + tires + keep + make('gone', 'Brand B') + '</Ads>')
    (folder / 'next' / 'avito_feed.xml').write_text('<Ads>' + tires + keep + '</Ads>')
    with pytest.raises(PublishError, match='бренда или точки'):
        check_before_publish(conn, account, folder / 'next' / 'avito_feed.xml', tmp_path)


def test_wheel_stock_is_not_duplicated_between_selected_addresses(tmp_path):
    import json
    from datetime import datetime
    import xml.etree.ElementTree as ET
    from helpers import catalog_xml, commodity_xml
    from test_cycle import NOW, TODAY, REF, prepared
    from tires_sync import connections, cycle, store
    from tires_sync.catalog import parse_reference
    conn = prepared(tmp_path, wheel_pricing={'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': True}},
                    wheel_accounts={'1': ['101', '102']})
    aid = connections.create_account(conn, 'Основной', NOW, 'owner')
    connections.save_addresses(conn, aid, 'percent', None,
        [{'name': 'A', 'address_id': '101', 'enabled': True, 'share': 50, 'address_text': ''},
         {'name': 'B', 'address_id': '102', 'enabled': True, 'share': 50, 'address_text': ''}], NOW, 'owner')
    store.set_state(conn, 'public_base', 'https://panel.example.test')
    path = tmp_path / 'wheels.xml'
    path.write_bytes(('<CATALOG>' + wheel(territory='T1', stock='4').replace('</COMMODITY>', '<STYPE>Лит.</STYPE></COMMODITY>') + '</CATALOG>').encode())
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat()}))
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, tmp_path / 'feed', wheel_path=path)
    assert result[0].ok, result[0].error
    account = connections.get_account(conn, aid)
    candidate = tmp_path / 'feed' / account['feed_token'] / 'next' / 'avito_feed.xml'
    ads = [a for a in ET.fromstring(candidate.read_bytes()) if (a.findtext('Id') or '').startswith('wheel_')]
    assert len(ads) == 1 and sum(int(a.findtext('Quantity')) for a in ads) == 4
    assert ads[0].findtext('SellerAddressID') in ('101', '102')


def test_promote_rejects_candidate_replaced_after_check(tmp_path):
    import hashlib
    import pytest
    from tires_sync.publish import promote_feed, PublishError
    folder = tmp_path / 'feed'
    (folder / 'next').mkdir(parents=True)
    candidate = folder / 'next' / 'avito_feed.xml'
    first = b'<Ads><Ad><Id>first</Id></Ad></Ads>'
    candidate.write_bytes(first)
    checked = hashlib.sha256(first).hexdigest()
    candidate.write_bytes(b'<Ads><Ad><Id>different</Id></Ad></Ads>')
    with pytest.raises(PublishError, match='изменился после проверки'):
        promote_feed(folder, expected_sha=checked)
    assert not (folder / 'avito_feed.xml').exists()


def _candidate_for_race_tests(tmp_path):
    import json
    from datetime import datetime
    from helpers import catalog_xml, commodity_xml
    from test_cycle import NOW, TODAY, REF, prepared
    from tires_sync import connections, cycle, store
    from tires_sync.catalog import parse_reference
    conn = prepared(tmp_path, wheel_pricing={'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': True}},
                    wheel_accounts={'1': ['101']})
    aid = connections.create_account(conn, 'Основной', NOW, 'owner')
    connections.set_live(conn, aid, True, NOW, 'owner')
    connections.save_addresses(conn, aid, 'percent', None,
        [{'name': 'Точка', 'address_id': '101', 'enabled': True, 'share': 100, 'address_text': 'СПб, Тестовая 1'}], NOW, 'owner', address_mode='text')
    store.set_state(conn, 'public_base', 'https://panel.example.test')
    path = tmp_path / 'wheels.xml'
    path.write_bytes(('<CATALOG>' + wheel(territory='T1', stock='100').replace('</COMMODITY>', '<STYPE>Лит.</STYPE></COMMODITY>') + '</CATALOG>').encode())
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat()}))
    root = tmp_path / 'feed'
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, root, wheel_path=path)
    assert result[0].ok, result[0].error
    return conn, aid, root, path


def test_changed_wheel_snapshot_cannot_publish_stale_stock(tmp_path):
    from tires_sync import publishing, connections
    conn, aid, root, path = _candidate_for_race_tests(tmp_path)
    path.write_bytes(path.read_bytes().replace(b'<NREST>100</NREST>', b'<NREST>4</NREST>'))
    result = publishing.publish_live_accounts(conn, root)
    assert not result[0]['ok'] and 'выгрузка дисков изменилась' in result[0]['message'].lower()
    assert not (root / connections.get_account(conn, aid)['feed_token'] / 'avito_feed.xml').exists()


def test_changed_wheel_markup_cannot_publish_stale_price(tmp_path):
    from tires_sync import publishing
    from tires_sync.settings_store import create_draft, confirm_draft
    conn, aid, root, path = _candidate_for_race_tests(tmp_path)
    draft = create_draft(conn, {'wheel_pricing': {'BRAND': {'pct_small': 9, 'pct_large': 15, 'enabled': True}}}, '2026-11-27T10:00:00')
    confirm_draft(conn, draft, '2026-11-27T10:01:00', 'owner')
    result = publishing.publish_live_accounts(conn, root)
    assert not result[0]['ok'] and 'настройки дисков изменились' in result[0]['message'].lower()


def test_duplicate_wheel_variants_are_not_published_twice():
    from dataclasses import replace
    from tires_sync.wheel_feed import build_ads
    item = parse_unload(('<CATALOG>' + wheel() + '</CATALOG>').encode(), '10').offers['42']
    same = replace(item, goods_id='43')
    rules = {'BRAND': {'pct_small': 10, 'pct_large': 10, 'enabled': True}}
    params = dict(account_id=1, address_id='2', phone='123', category='parts', product_type='wheels', rim_types={'': 'type'},
                  photo_for=lambda w: 'https://example.test/a.jpg', photo_prefix='https://example.test/')
    ads, rejected = build_ads([item, same], rules, {'BRAND'}, **params)
    assert not ads and rejected == {'42': ['duplicate'], '43': ['duplicate']}
    different = replace(same, color='black')
    ads, rejected = build_ads([item, different], rules, {'BRAND'}, **params)
    assert len(ads) == 2 and not rejected


def test_unrelated_setting_change_does_not_block_wheel_feed(tmp_path):
    from tires_sync import publishing
    from tires_sync.settings_store import create_draft, confirm_draft
    conn, aid, root, path = _candidate_for_race_tests(tmp_path)
    draft = create_draft(conn, {'date_end_days': 31}, '2026-11-27T10:00:00')
    confirm_draft(conn, draft, '2026-11-27T10:01:00', 'owner')
    result = publishing.publish_live_accounts(conn, root)
    assert result[0]['ok'], result[0]['message']


def test_confirmed_brand_disable_allows_removal(tmp_path):
    from helpers import catalog_xml, commodity_xml
    from test_cycle import REF, TODAY
    from tires_sync import publishing, cycle, connections
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import create_draft, confirm_draft
    conn, aid, root, path = _candidate_for_race_tests(tmp_path)
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    draft = create_draft(conn, {'wheel_pricing': {'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': False}}}, '2026-11-27T10:00:00')
    confirm_draft(conn, draft, '2026-11-27T10:01:00', 'owner')
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, root, wheel_path=path)
    assert result[0].ok, result[0].error
    result = publishing.publish_live_accounts(conn, root)
    assert result[0]['ok'], result[0]['message']
    import xml.etree.ElementTree as ET
    live = root / connections.get_account(conn, aid)['feed_token'] / 'avito_feed.xml'
    assert not any((a.findtext('Id') or '').startswith('wheel_') for a in ET.parse(live).getroot())


def test_stock_only_pass_updates_wheels_from_new_snapshot(tmp_path):
    import json
    import xml.etree.ElementTree as ET
    from datetime import datetime, date
    from test_stock_pass import NOW, REF, FIVE, AFTER
    from tires_sync import connections, cycle, store, publishing
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import init_settings
    from tires_sync.webmim import parse_unload as parse_tires
    from helpers import settings_dict

    conn = store.connect(tmp_path / 't.db')
    init_settings(conn, settings_dict(wheel_pricing={'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': True}},
                                      wheel_accounts={'1': ['101']}), NOW)
    account_id = connections.create_account(conn, 'Основной', NOW, 'owner')
    connections.set_live(conn, account_id, True, NOW, 'owner')
    connections.save_addresses(conn, account_id, 'percent', None,
                               [{'name': 'Точка', 'address_id': '101', 'enabled': True, 'share': 100,
                                 'address_text': 'Санкт-Петербург, Тестовая 1'}], NOW, 'owner', address_mode='text')
    store.set_state(conn, 'public_base', 'https://panel.example.test')
    path = tmp_path / 'wheels.xml'
    path.write_bytes(('<CATALOG>' + wheel(territory='T1').replace('</COMMODITY>', '<STYPE>Лит.</STYPE></COMMODITY>') + '</CATALOG>').encode())
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat(timespec='seconds')}))
    root, today, ref = tmp_path / 'feed', date(2026, 10, 24), parse_reference(REF)
    initial = cycle.run_for_accounts(conn, FIVE, ref, today, root, wheel_path=path)
    assert initial[0].ok, initial[0].error
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    account = connections.get_account(conn, account_id)
    public = root / account['feed_token'] / 'avito_feed.xml'
    original = next(a for a in ET.fromstring(public.read_bytes()) if (a.findtext('Id') or '').startswith('wheel_'))
    path.write_bytes(path.read_bytes().replace(b'<NREST>10</NREST>', b'<NREST>4</NREST>'))
    result = cycle.stock_pass(conn, parse_tires(AFTER, 'T1'), ref, today, root)
    assert result and result[0].ok, result[0].error if result else 'нет результата'
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    saved = next(a for a in ET.fromstring(public.read_bytes()) if (a.findtext('Id') or '').startswith('wheel_'))
    assert int(saved.findtext('Quantity')) == 4
    assert int(original.findtext('Quantity')) >= 4


def test_missing_snapshot_cannot_certify_old_prices_after_markup_change(tmp_path):
    from helpers import catalog_xml, commodity_xml
    from test_cycle import REF, TODAY
    from tires_sync import publishing, cycle
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import create_draft, confirm_draft
    conn, aid, root, path = _candidate_for_race_tests(tmp_path)
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    draft = create_draft(conn, {'wheel_pricing': {'BRAND': {'pct_small': 12, 'pct_large': 15, 'enabled': True}}}, '2026-11-27T10:00:00')
    confirm_draft(conn, draft, '2026-11-27T10:01:00', 'owner')
    path.unlink()
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, root, wheel_path=path)
    assert not result[0].ok
    assert 'выгрузка МИМ недоступна' in result[0].error


def test_out_of_stock_last_wheel_is_removed_without_blocking_tires(tmp_path):
    from helpers import catalog_xml, commodity_xml
    from test_cycle import REF, TODAY
    from tires_sync import publishing, cycle, connections
    from tires_sync.catalog import parse_reference
    import xml.etree.ElementTree as ET
    conn, aid, root, path = _candidate_for_race_tests(tmp_path)
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    path.write_bytes(path.read_bytes().replace(b'<NREST>100</NREST>', b'<NREST>0</NREST>'))
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NPRICE_DELAYED='5500')), parse_reference(REF), TODAY, root, wheel_path=path)
    assert result[0].ok, result[0].error
    result = publishing.publish_live_accounts(conn, root)
    assert result[0]['ok'], result[0]['message']
    live = root / connections.get_account(conn, aid)['feed_token'] / 'avito_feed.xml'
    ads = ET.parse(live).getroot().findall('Ad')
    assert ads and not any((ad.findtext('Id') or '').startswith('wheel_') for ad in ads)


def test_confirmed_disable_works_when_wheels_are_most_of_feed(tmp_path):
    from helpers import catalog_xml, commodity_xml
    from test_cycle import REF, TODAY
    from tires_sync import publishing, cycle
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import create_draft, confirm_draft
    conn, aid, root, path = _candidate_for_race_tests(tmp_path)
    entries = []
    for number in range(5):
        entry = wheel(str(42 + number), territory='T1', stock='100').replace('<SDIAMETR>16</SDIAMETR>', '<SDIAMETR>%d</SDIAMETR>' % (16 + number))
        entries.append(entry.replace('</COMMODITY>', '<STYPE>Лит.</STYPE></COMMODITY>'))
    path.write_bytes(('<CATALOG>' + ''.join(entries) + '</CATALOG>').encode())
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, root, wheel_path=path)
    assert result[0].ok, result[0].error
    assert publishing.publish_live_accounts(conn, root)[0]['ok']
    draft = create_draft(conn, {'wheel_pricing': {'BRAND': {'pct_small': 10, 'pct_large': 15, 'enabled': False}}}, '2026-11-27T10:00:00')
    confirm_draft(conn, draft, '2026-11-27T10:01:00', 'owner')
    result = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, root, wheel_path=path)
    assert result[0].ok, result[0].error
    result = publishing.publish_live_accounts(conn, root)
    assert result[0]['ok'], result[0]['message']
