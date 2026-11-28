from test_catalog_filters import setup, as_user
from test_webmim_wheels import wheel
from tires_sync import connections
from tires_sync.fetcher import wheel_snapshot_path
from tires_sync.settings_store import active_settings, create_draft, confirm_draft


def world(tmp_path):
    app, owner, _, conn = setup(tmp_path)
    territory = connections.supplier_territory(conn, 1, active_settings(conn))
    def item(gid, name, width, diameter, brand, stock, cost, kind):
        return (wheel(gid, territory=territory, stock=stock, price=cost)
                .replace('<SMODIFNAME>Disk</SMODIFNAME>', '<SMODIFNAME>' + name + '</SMODIFNAME>')
                .replace('<SMARKA>Brand</SMARKA>', '<SMARKA>' + brand + '</SMARKA>')
                .replace('<SWIDTH>6,5</SWIDTH>', '<SWIDTH>' + width + '</SWIDTH>')
                .replace('<SDIAMETR>16</SDIAMETR>', '<SDIAMETR>' + diameter + '</SDIAMETR>')
                .replace('</COMMODITY>', '<STYPE>' + kind + '</STYPE></COMMODITY>'))
    data = '<CATALOG>' + item('41', 'Silver rim', '6,5', '16', 'Brand', '4', '1000', 'Лит.') + item('42', 'Black rim', '7', '17', 'Other', '2', '2000', 'Штамп.') + item('43', 'Out of stock', '6,5', '16', 'Brand', '0', '1000', 'Лит.') + '</CATALOG>'
    wheel_snapshot_path(app.config['UNLOAD_PATH']).write_text(data, encoding='utf-8')
    draft = create_draft(conn, {'wheel_pricing': {'BRAND': {'pct_small': 10, 'pct_large': 10, 'enabled': False}, 'OTHER': {'pct_small': 10, 'pct_large': 10, 'enabled': False}}}, '2026-11-27T10:00:00')
    confirm_draft(conn, draft, '2026-11-27T10:01:00', 'owner')
    return app, owner, conn


def test_filters_combine_and_normalize_wheel_parameters(tmp_path):
    app, client, conn = world(tmp_path)
    page = client.get('/catalog?category=wheels&width=6.5&diameter=16&holes=5&pcd=114.3&offset=40&bore=66.1&type=Лит.&brand=Brand').get_data(as_text=True)
    assert 'Silver rim' in page and 'Black rim' not in page and 'Out of stock' not in page
    assert 'value="6,5" selected' in page
    assert 'Указанные параметры совпадают точно' not in page
    assert 'Происхождение недоступно' not in page
    assert client.get('/catalog?category=wheels&q=42').get_data(as_text=True).count('Black rim') == 1
    assert 'Black rim' not in client.get('/catalog?category=wheels&width=bad').get_data(as_text=True)


def test_stock_price_extra_and_reset_controls(tmp_path):
    app, client, conn = world(tmp_path)
    page = client.get('/catalog?category=wheels&sn=0&pf=1050&pt=1150').get_data(as_text=True)
    assert 'Silver rim' in page and 'Out of stock' in page and 'Black rim' not in page
    assert 'data-wheel-details open' in page
    assert 'wheel-reset is-disabled' in client.get('/catalog?category=wheels').get_data(as_text=True)
    assert 'wheel-reset is-disabled' not in page


def test_supplier_price_not_exposed_without_money_right(tmp_path):
    app, owner, conn = world(tmp_path)
    viewer = as_user(app, conn, 'viewer_wheels', 'viewer')
    page = viewer.get('/catalog?category=wheels').get_data(as_text=True)
    assert 'Silver rim' in page and 'Цена поставщика' not in page
    assert 'Цена поставщика' in owner.get('/catalog?category=wheels').get_data(as_text=True)


def test_collapsed_extra_filters_stay_collapsed_and_unknown_option_is_visible(tmp_path):
    app, client, conn = world(tmp_path)
    page = client.get('/catalog?category=wheels&width=8&pf=100&more=0').get_data(as_text=True)
    assert 'value="8" selected' in page
    assert 'data-wheel-details open' not in page
    assert 'По выбранным условиям диски не найдены' in page


def test_vehicle_selection_filters_by_bolt_pattern_and_keeps_other_filters(tmp_path, monkeypatch):
    from tires_sync.panel import vehicle_fitment
    records = [
        {'make': 'Example', 'model': 'Example One', 'year': '2020-2022', 'chassis_code': 'A',
         'bolt_pattern': '5x114.3', 'center_bore_mm': '66.1', 'oe_wheel': '16x6.5 ET40'},
        {'make': 'Example', 'model': 'Example One', 'year': '2020-2022', 'chassis_code': 'B',
         'bolt_pattern': '5x100', 'center_bore_mm': '57.1', 'oe_wheel': ''},
    ]
    monkeypatch.setattr(vehicle_fitment, 'dataset', lambda: records)
    app, client, _ = world(tmp_path)
    base = '/catalog?category=wheels&car_make=Example&car_year=2021&car_model=Example+One'
    page = client.get(base).get_data(as_text=True)
    assert 'Модификация автомобиля' in page and '5x114.3' in page
    selected = vehicle_fitment.selection({'car_make': 'Example', 'car_year': '2021', 'car_model': 'Example One'})
    index = next(i for i, v in enumerate(selected['variants']) if v[1] == '5x100')
    page = client.get(base + '&car_variant=' + str(index)).get_data(as_text=True)
    assert 'Silver rim' not in page and 'Black rim' not in page
    index = next(i for i, v in enumerate(selected['variants']) if v[1] == '5x114.3')
    page = client.get(base + '&car_variant=' + str(index) + '&brand=Brand').get_data(as_text=True)
    assert 'Silver rim' in page and 'Black rim' not in page
    assert 'Проверьте совместимость перед установкой' in page
    assert 'ANKUSI Wheel Fitment Dataset' in page


def test_vehicle_dataset_is_available_and_untrusted_selection_is_safe():
    from tires_sync.panel import vehicle_fitment
    assert len(vehicle_fitment.dataset()) > 10000
    assert vehicle_fitment.selection({'car_make': 'BMW', 'car_year': '2020', 'car_model': 'missing', 'car_variant': '0'})['variant'] is None
    assert vehicle_fitment.bolt_pattern(('A', 'invalid', '', '')) is None


def test_wheel_name_does_not_repeat_brand():
    from types import SimpleNamespace
    from tires_sync.panel.wheel_catalog import display_name
    assert display_name(SimpleNamespace(name='BRAND Model 16', brand='Brand')) == 'Model 16'
    assert display_name(SimpleNamespace(name='Brandstone Model', brand='Brand')) == 'Brandstone Model'
    assert display_name(SimpleNamespace(name='Brand', brand='Brand')) == 'Brand'


def test_wheel_status_uses_shared_catalog_component_and_truthful_state(tmp_path):
    from datetime import datetime
    import json
    from tires_sync import staging
    from tires_sync.fetcher import wheel_snapshot_path
    app, client, conn = world(tmp_path)
    connections.create_account(conn, 'Основной', '2026-11-28T10:00:00', 'owner')
    path = wheel_snapshot_path(app.config['UNLOAD_PATH'])
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat()}))
    page = client.get('/catalog?category=wheels').get_data(as_text=True)
    assert 'st-ico--neutral' in page and 'data-tip-title="Бренд не включён"' in page
    assert '/catalog/wheels/41/photo' in page and 'href="/catalog/wheels/41"' in page
    assert client.get('/catalog/wheels/41').status_code == 200
    card = client.get('/catalog/wheels/41').get_data(as_text=True)
    assert 'Silver rim' in card and 'Разболтовка' in card and '/catalog/wheels/41/photo' in card
    assert client.get('/catalog/wheels/99999').status_code == 404
    staging.set_override(conn, 'w-41', 'on', '2026-11-28T10:01:00')
    page = client.get('/catalog?category=wheels').get_data(as_text=True)
    assert 'st-ico--pend' in page and 'Станет:' in page


def test_wheel_status_does_not_claim_unpublished_manual_item_is_in_feed():
    from types import SimpleNamespace
    from tires_sync.panel.wheel_catalog import status
    row = {'wheel': SimpleNamespace(stock=4), 'unverified': False, 'gone': False,
           'manual': 'on', 'manual_pending': False, 'price': 1000, 'brand_enabled': False, 'published': False}
    assert status(row, 1)['reason'] == 'не в фиде'
    row['published'] = True
    assert status(row, 1)['reason'] == 'добавлено вручную'
    row['wheel'].stock = 0
    assert status(row, 1)['reason'] == 'остаток ниже порога'


def test_catalog_wheel_photo_uses_supplier_proxy_and_requires_catalog_access(tmp_path):
    from test_account_photo import JPEG
    app, owner, conn = world(tmp_path)
    connections.create_account(conn, 'Основной', '2026-11-28T10:00:00', 'owner')
    app.config['PHOTO_FETCH'] = lambda url: (JPEG, 'jpg')
    response = owner.get('/catalog/wheels/41/photo')
    assert response.status_code == 200 and response.data == JPEG
    assert response.mimetype == 'image/jpeg'
    assert owner.get('/catalog/wheels/invalid/photo').status_code == 404


def test_wheel_status_marks_brand_draft_and_keeps_last_feed_fact():
    from types import SimpleNamespace
    from tires_sync.panel.wheel_catalog import status
    row = {'wheel': SimpleNamespace(stock=4), 'unverified': False, 'gone': False,
           'manual': 'off', 'manual_pending': False, 'price': 1000, 'brand_enabled': True, 'published': True}
    assert status(row, 1)['scode'] == 'manual_off'


def test_wheel_brand_draft_is_shown_as_pending(tmp_path):
    from datetime import datetime
    import json
    from tires_sync import staging
    app, client, conn = world(tmp_path)
    connections.create_account(conn, 'Основной', '2026-11-28T10:00:00', 'owner')
    path = wheel_snapshot_path(app.config['UNLOAD_PATH'])
    path.with_name(path.name + '.meta.json').write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat()}))
    staging.set_wheel_pricing(conn, 'BRAND', 10, 10, '2026-11-28T10:01:00', enabled=True)
    page = client.get('/catalog?category=wheels').get_data(as_text=True)
    assert 'st-ico--pend' in page and 'Станет:' in page
