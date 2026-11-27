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
    assert 'Диски: калькуляция и публикация' in html and 'Подготовить к публикации' in html
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
