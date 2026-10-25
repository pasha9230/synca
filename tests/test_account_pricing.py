from __future__ import annotations

from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, make_settings, settings_dict
from panel_helpers import login, make_app, post
from tires_sync import connections, cycle, store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import init_settings

NOW = "2026-10-14T10:00:00"
REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')


def test_pricing_and_stock_threshold_are_stored_per_account_with_validation(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    a = connections.create_account(conn, "A", NOW, "owner")
    assert connections.get_account_pricing(conn, a) == {"delta": 0.0, "min_stock": None}
    connections.set_account_pricing(conn, a, 2.5, 6, NOW, "owner")
    assert connections.get_account_pricing(conn, a) == {"delta": 2.5, "min_stock": 6}
    connections.set_account_pricing(conn, a, 0, None, NOW, "owner")
    assert connections.get_account_pricing(conn, a) == {"delta": 0.0, "min_stock": None}
    for delta, stock in ((11, None), (-11, None), (float("nan"), None), (0, -1), (0, 1001)):
        with pytest.raises(ValueError):
            connections.set_account_pricing(conn, a, delta, stock, NOW, "owner")
    assert any(e["action"] == "account_pricing" for e in store.list_audit(conn, 10))


def test_effective_settings_apply_the_account_delta_to_every_rule_and_its_own_threshold(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    a = connections.create_account(conn, "A", NOW, "owner")
    general = make_settings()
    connections.set_account_pricing(conn, a, 2.0, 7, NOW, "owner")
    eff = connections.effective_settings(conn, a, general)
    rule, base = eff.pricing[("IKON TYRES", list(general.pricing)[0][1])], general.pricing[list(general.pricing)[0]]
    assert rule.pct_small == pytest.approx(base.pct_small + 2.0) and rule.pct_large == pytest.approx(base.pct_large + 2.0)
    assert eff.min_stock == 7 and general.min_stock == 4


def test_two_accounts_get_their_own_prices_from_the_same_supplier_data(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    first = connections.create_account(conn, "A", NOW, "owner")
    second = connections.create_account(conn, "B", NOW, "owner")
    connections.set_account_pricing(conn, second, 2.0, None, NOW, "owner")
    xml = catalog_xml(commodity_xml(NPRICE_DELAYED="5000"))
    cycle.run_for_accounts(conn, xml, REF, date(2026, 10, 14), tmp_path / "feed")
    text = {a: (tmp_path / "feed" / connections.get_account(conn, a)["feed_token"] / "next" / "avito_feed.xml").read_text(encoding="utf-8")
            for a in (first, second)}
    assert "<Price>4810</Price>" in text[first] and "<Price>4910</Price>" in text[second]


def test_account_page_has_a_form_for_the_account_pricing(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    connections.create_account(store.connect(app.config["DB_PATH"]), "A", NOW, "owner")
    html = client.get("/connections/avito/1").get_data(as_text=True)
    assert "Цены и остатки аккаунта" in html and 'name="delta"' in html and 'name="min_stock"' in html
    assert post(client, "/connections/accounts/1/pricing", tok, delta="1.5", min_stock="6").status_code == 302
    conn = store.connect(app.config["DB_PATH"])
    assert connections.get_account_pricing(conn, 1) == {"delta": 1.5, "min_stock": 6}
    post(client, "/connections/accounts/1/pricing", tok, delta="99", min_stock="")
    assert connections.get_account_pricing(conn, 1)["delta"] == 1.5
    assert "от −10 до +10" in client.get("/connections/avito/1").get_data(as_text=True)
    assert client.post("/connections/accounts/9/pricing", data={"csrf": tok}).status_code == 404
