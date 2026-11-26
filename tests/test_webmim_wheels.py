from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from http_fakes import ScriptedHttp
from tires_sync.webmim_client import WebmimClient
from tires_sync.webmim_wheels import parse_unload


def wheel(gid='42', territory='10', price='1234,50', stock='4'):
    return ('<COMMODITY><NNOMMODIF>' + gid + '</NNOMMODIF><TERRITORY_RN>' + territory +
            '</TERRITORY_RN><SMODIFNAME>Disk</SMODIFNAME><SMARKA>Brand</SMARKA>'
            '<SDIAMETR>16</SDIAMETR><SWIDTH>6,5</SWIDTH><SHOLESQUANT>5</SHOLESQUANT>'
            '<SPCD>114,3</SPCD><SWHEELOFFSET>40</SWHEELOFFSET><SDIA>66,1</SDIA>'
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
    assert 'Disk' not in owner.get('/catalog').get_data(as_text=True)


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
    params = dict(account_id=10, address_id='25', phone='123', category='Запчасти и аксессуары',
                  product_type='Диски', rim_types={'': 'Литые'})
    rules = {'BRAND': {'pct_small': 10, 'pct_large': 15}}
    assert build_ads([wheel_item], rules, set(), **params) == ([], {})
    ads, rejected = build_ads([wheel_item], rules, {'BRAND'}, **params)
    assert not rejected and len(ads) == 1
    ad = ads[0]
    assert ad.findtext('RimBoltsDiameter') == '114,3' and ad.findtext('Price') == '1400'
    template = __import__('xml.etree.ElementTree', fromlist=['parse']).parse(str(
        Path(__file__).parents[1] / 'src/tires_sync/data/avito-templates/diski-2026-11-26.xml')).getroot()[0]
    assert {e.tag for e in ad} <= {e.tag for e in template}
    other, _ = build_ads([wheel_item], rules, {'BRAND'}, **dict(params, account_id=11))
    assert ad.findtext('Id') != other[0].findtext('Id')


def test_wheel_feed_rejects_nonpositive_prices():
    from tires_sync.wheel_feed import build_ads
    item = parse_unload(('<CATALOG>' + wheel() + '</CATALOG>').encode(), '10').offers['42']
    rules = {'BRAND': {'pct_small': -200, 'pct_large': 0}}
    ads, rejected = build_ads([item], rules, {'BRAND'}, account_id=1, address_id='2', phone='123',
                              category='parts', product_type='wheels', rim_types={'': 'type'})
    assert ads == [] and rejected == {'42': ['price']}


def test_wheel_account_settings_staged_per_account_and_validate_inputs(tmp_path):
    import pytest
    from test_settings_store import fresh, T2
    from tires_sync import staging
    conn = fresh(tmp_path)
    staging.set_wheel_account(conn, 12, ['loc1', 'loc2'], T2)
    assert staging.effective_raw(conn)['wheel_accounts'] == {'12': ['loc1', 'loc2']}
    staging.set_wheel_account(conn, 12, [], T2)
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
