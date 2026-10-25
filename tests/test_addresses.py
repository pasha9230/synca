from __future__ import annotations

from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml, make_settings, settings_dict
from tires_sync import connections, cycle, store
from tires_sync.catalog import parse_reference
from tires_sync.settings_store import init_settings

NOW = "2026-10-10T12:00:00"
REF = '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>'


def rows(*items):
    return [{"name": n, "address_id": a, "enabled": e, "share": s} for n, a, e, s in items]


def setup(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    first = connections.create_account(conn, "Основной", NOW, "owner")
    second = connections.create_account(conn, "Второй", NOW, "owner")
    return conn, first, second


@pytest.mark.usefixtures("no_own_address")                 # these tests describe an account that has no address of its own yet
def test_addresses_are_stored_per_account_and_audited(tmp_path):
    conn, a, b = setup(tmp_path)
    connections.save_addresses(conn, a, "percent", None, rows(("Бета", "101", True, 60), ("Гамма", "102", True, 40)), NOW, "owner")
    assert [(r["name"], r["address_id"], r["share"]) for r in connections.list_addresses(conn, a)] == [("Бета", "101", 60.0), ("Гамма", "102", 40.0)]
    assert connections.list_addresses(conn, b) == []
    assert connections.get_point_config(conn, a) == ("percent", None)
    assert any(e["action"] == "account_addresses" for e in store.list_audit(conn, 10))


def test_saving_replaces_the_whole_list(tmp_path):
    conn, a, _ = setup(tmp_path)
    connections.save_addresses(conn, a, "percent", None, rows(("Бета", "101", True, 100)), NOW, "owner")
    connections.save_addresses(conn, a, "percent", None, rows(("Гамма", "102", True, 100)), NOW, "owner")
    assert [r["address_id"] for r in connections.list_addresses(conn, a)] == ["102"]


@pytest.mark.parametrize("bad, text", [
    (rows(("A", "", True, 100)), "идентификатор"),
    (rows(("A", "12x", True, 100)), "идентификатор"),
    (rows(("A", "101", True, 50), ("B", "101", True, 50)), "повтор"),
    (rows(("A", "101", True, 50), ("B", "102", True, 40)), "100"),
    (rows(("A", "101", False, 100)), "включ"),
    (rows(("A", "101", True, float("nan"))), "доля"),
    (rows(("A", "101", True, -5), ("B", "102", True, 105)), "доля"),
    ([], "включ"),
])
def test_invalid_address_lists_are_rejected(tmp_path, bad, text):
    conn, a, _ = setup(tmp_path)
    with pytest.raises(ValueError) as err:
        connections.save_addresses(conn, a, "percent", None, bad, NOW, "owner")
    assert text in str(err.value) and connections.list_addresses(conn, a) == []


def test_count_mode_needs_a_known_rest_point(tmp_path):
    conn, a, _ = setup(tmp_path)
    ok = rows(("A", "101", True, 10), ("B", "102", True, 5))
    connections.save_addresses(conn, a, "count", "102", ok, NOW, "owner")
    with pytest.raises(ValueError):
        connections.save_addresses(conn, a, "count", "999", ok, NOW, "owner")


@pytest.mark.usefixtures("no_own_address")                 # these tests describe an account that has no address of its own yet
def test_effective_settings_use_enabled_addresses_or_fall_back_to_the_general_ones(tmp_path):
    conn, a, b = setup(tmp_path)
    general = make_settings()
    assert connections.effective_settings(conn, b, general).point_values == general.point_values
    connections.save_addresses(conn, a, "percent", None, rows(("A", "101", True, 70), ("B", "102", True, 30), ("C", "103", False, 0)), NOW, "owner")
    eff = connections.effective_settings(conn, a, general)
    assert eff.point_values == {"101": 70.0, "102": 30.0} and eff.min_stock == general.min_stock


def test_each_account_feed_uses_its_own_addresses(tmp_path):
    conn, a, b = setup(tmp_path)
    connections.save_addresses(conn, a, "percent", None, rows(("A", "101", True, 100)), NOW, "owner")
    connections.save_addresses(conn, b, "percent", None, rows(("B", "202", True, 100)), NOW, "owner")
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), date(2026, 10, 10), tmp_path / "feed")
    feeds = {acc["id"]: (tmp_path / "feed" / acc["feed_token"] / "next" / "avito_feed.xml").read_text(encoding="utf-8")
             for acc in connections.list_accounts(conn)}
    assert "<SellerAddressID>101</SellerAddressID>" in feeds[a] and "<SellerAddressID>202<" not in feeds[a]
    assert "<SellerAddressID>202</SellerAddressID>" in feeds[b] and "<SellerAddressID>101<" not in feeds[b]


def test_text_address_mode_puts_the_full_address_in_the_feed_instead_of_the_id(tmp_path):
    conn, a, b = setup(tmp_path)
    r = [{"name": "Бета", "address_id": "101", "enabled": True, "share": 100, "address_text": "Санкт-Петербург, ул. Генерала Примерова, 35А"}]
    connections.save_addresses(conn, a, "percent", None, r, NOW, "owner", address_mode="text")
    assert connections.list_addresses(conn, a)[0]["address_text"] == "Санкт-Петербург, ул. Генерала Примерова, 35А"
    assert connections.get_address_mode(conn, a) == "text"
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), date(2026, 10, 10), tmp_path / "feed")
    feed = (tmp_path / "feed" / connections.get_account(conn, a)["feed_token"] / "next" / "avito_feed.xml").read_text(encoding="utf-8")
    assert "<Address>Санкт-Петербург, ул. Генерала Примерова, 35А</Address>" in feed and "SellerAddressID" not in feed


def test_text_address_mode_requires_the_text_for_enabled_addresses(tmp_path):
    conn, a, _ = setup(tmp_path)
    with pytest.raises(ValueError) as err:
        connections.save_addresses(conn, a, "percent", None, rows(("A", "101", True, 100)), NOW, "owner", address_mode="text")
    assert "полный адрес" in str(err.value)
    with pytest.raises(ValueError):
        connections.save_addresses(conn, a, "percent", None, rows(("A", "101", True, 100)), NOW, "owner", address_mode="bogus")


def test_a_brand_rule_sends_new_ads_of_that_brand_to_the_chosen_address(tmp_path):
    conn, a, _ = setup(tmp_path)
    connections.save_addresses(conn, a, "percent", None, rows(("A", "101", True, 50), ("B", "102", True, 50)), NOW, "owner")
    xml = catalog_xml(*[commodity_xml(NNOMMODIF=str(i)) for i in range(100, 106)])
    cycle.run_for_accounts(conn, xml, parse_reference(REF), date(2026, 10, 10), tmp_path / "feed")
    path = tmp_path / "feed" / connections.get_account(conn, a)["feed_token"] / "next" / "avito_feed.xml"
    assert "<Address>" not in path.read_text(encoding="utf-8") and path.read_text(encoding="utf-8").count("<SellerAddressID>101<") == 3
    connections.set_point_rule(conn, a, "IKON TYRES", "winter", "102", NOW, "owner")
    assert connections.list_point_rules(conn, a) == [{"brand_key": "IKON TYRES", "season": "winter", "address_id": "102"}]
    cycle.run_for_accounts(conn, xml, parse_reference(REF), date(2026, 10, 10), tmp_path / "feed")
    text = path.read_text(encoding="utf-8")
    assert text.count("<SellerAddressID>102<") == 6 and "<SellerAddressID>101<" not in text


def test_a_rule_needs_an_enabled_address_of_the_same_account_and_can_be_cleared(tmp_path):
    conn, a, b = setup(tmp_path)
    connections.save_addresses(conn, a, "percent", None, rows(("A", "101", True, 100)), NOW, "owner")
    with pytest.raises(ValueError):
        connections.set_point_rule(conn, a, "IKON TYRES", "winter", "999", NOW, "owner")
    with pytest.raises(ValueError):
        connections.set_point_rule(conn, b, "IKON TYRES", "winter", "101", NOW, "owner")
    connections.set_point_rule(conn, a, "IKON TYRES", "winter", "101", NOW, "owner")
    connections.clear_point_rule(conn, a, "IKON TYRES", "winter", NOW, "owner")
    assert connections.list_point_rules(conn, a) == []
    assert any(e["action"] == "point_rule" for e in store.list_audit(conn, 10))
