from __future__ import annotations

import pytest

from helpers import catalog_xml, commodity_xml, make_settings
from tires_sync import connections, store
from tires_sync.catalog import resolve_brand
from tires_sync.webmim import merge_parsed, parse_unload

NOW = "2026-10-14T10:00:00"


def test_supplier_one_always_exists_and_new_ones_get_the_next_ids(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    assert [(s["id"], s["name"]) for s in connections.list_suppliers(conn)] == [(1, "WebMIM")]
    two = connections.create_supplier(conn, "Второй поставщик", NOW, "owner")
    three = connections.create_supplier(conn, "Третий", NOW, "owner")
    assert (two, three) == (2, 3)
    assert connections.get_supplier(conn, 2)["name"] == "Второй поставщик"
    assert connections.get_supplier(conn, 99) is None
    with pytest.raises(ValueError):
        connections.create_supplier(conn, "   ", NOW, "owner")


def test_supplier_one_keeps_the_existing_connection_row_and_others_use_their_id():
    assert connections.supplier_owner(1) == 0 and connections.supplier_owner(2) == 2 and connections.supplier_owner(7) == 7


def test_territory_of_supplier_one_comes_from_general_settings_others_from_their_own_config(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    s = make_settings(territory_rn="T1")
    assert connections.supplier_territory(conn, 1, s) == "T1"
    two = connections.create_supplier(conn, "Второй", NOW, "owner")
    with pytest.raises(ValueError):
        connections.supplier_territory(conn, two, s)
    connections.set_config(conn, "webmim", connections.supplier_owner(two),
                           {"base_url": "https://b.example", "mode": "apikey", "territory_rn": "T9"}, NOW, "owner")
    assert connections.supplier_territory(conn, two, s) == "T9"


def rows(**kw):
    return catalog_xml(commodity_xml(NNOMMODIF="5", TERRITORY_RN="T1", **kw))


def test_other_suppliers_get_their_own_namespace_so_equal_articles_never_collide():
    one = parse_unload(rows(), "T1")
    two = parse_unload(rows(), "T1", supplier=2)
    assert list(one.offers) == ["5"] and one.offers["5"].brand_key == "IKON TYRES"
    assert list(two.offers) == ["s2-5"]
    offer = two.offers["s2-5"]
    assert offer.goods_id == "s2-5" and offer.brand_key == "S2:IKON TYRES"
    merged = merge_parsed([one, two])
    assert set(merged.offers) == {"5", "s2-5"}


def test_merge_keeps_issues_and_other_territories_of_every_supplier():
    bad = parse_unload(catalog_xml(commodity_xml(NNOMMODIF="6", TERRITORY_RN="T1", SDIAMETR="x")), "T1", supplier=2)
    other = parse_unload(catalog_xml(commodity_xml(NNOMMODIF="7", TERRITORY_RN="ZZ")), "T1")
    merged = merge_parsed([parse_unload(rows(), "T1"), bad, other])
    assert "s2-6" in merged.issues and merged.other_territories == {"ZZ": 1}


def test_the_namespace_prefix_is_ignored_when_matching_the_avito_brand():
    ref = {"Ikon Tyres": frozenset({"X"})}
    assert resolve_brand("IKON TYRES", ref) == "Ikon Tyres"
    assert resolve_brand("S2:IKON TYRES", ref) == "Ikon Tyres"
    assert resolve_brand("S2:IKON TYRES", ref, {"IKON TYRES": "Ikon Tyres"}) == "Ikon Tyres"
