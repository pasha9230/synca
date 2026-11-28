from decimal import Decimal
from types import SimpleNamespace

import pytest

from tires_sync import wheel_templates as templates


def wheel(**overrides):
    attrs = {"goods_id": "12", "name": "Диск X R16", "brand": "X", "model": "", "width": "7",
             "diameter": "16", "holes": "5", "pcd": "114.3", "offset": "40", "center_bore": "67.1",
             "wheel_type": "Лит.", "color": "black", "stock": 4, "code": "A1"}
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def test_wheel_template_uses_product_values_and_omits_missing_lines():
    text = "{name}\nБренд: {brand}\nМодель: {model}\n{holes}x{pcd}\nЦена: {price}"
    assert templates.render(text, wheel(), 5000) == "Диск X R16\nБренд: X\n5x114.3\nЦена: 5000"
    assert templates.render(templates.DEFAULT_TITLE, wheel(), 5000, title=True) == "Диск X R16"


@pytest.mark.parametrize("text", ["{unknown}", "{brand.__class__}", "{price!r}", "{price:1000}", "{brand", "x\x01"])
def test_bad_expressions_are_rejected(text):
    assert templates.validate(text)
    with pytest.raises(ValueError):
        templates.render(text, wheel(), 5000)


def test_cannot_render_empty_or_oversized_title():
    assert templates.render("{model}", wheel(), Decimal("5000"), title=True) == "Диск X R16"
    assert len(templates.render("{name}", wheel(name="x" * 201), 5000, title=True)) == 200


def test_settings_validation():
    templates.validate_settings({"title": "{brand} {diameter}", "description": "{name}"})
    with pytest.raises(ValueError):
        templates.validate_settings({"title": "{brand!r}"})
    with pytest.raises(ValueError):
        templates.validate_settings({"title": 123})


def test_templates_are_staged_and_require_permission(tmp_path):
    from test_wheel_own_prices import setup_world
    from test_catalog_filters import as_user
    from tires_sync import staging
    from tires_sync.settings_store import get_active

    app, client, conn, _, _ = setup_world(tmp_path)
    page = client.get('/settings/brands').get_data(as_text=True)
    assert 'Диски: название и описание для Авито' in page
    data = {'csrf': 'tok', 'scope': 'wheel_templates', 'rev': staging.rev({}),
            'title': '{brand} R{diameter}', 'description': '{name}\nЦена: {price}'}
    viewer = as_user(app, conn, 'wheeltemplateviewer', 'viewer')
    assert viewer.post('/settings/pricing', data=data).status_code == 403
    assert client.post('/settings/pricing', data=data).status_code == 302
    assert staging.effective_raw(conn)['wheel_templates'] == {'title': data['title'], 'description': data['description']}
    assert not get_active(conn)[1].get('wheel_templates')
