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
