"""Commercial tires (C, LT) are not passenger tires: they are not taken from the supplier's unload at all (owner's decision, 2026-10-14)."""
from __future__ import annotations

from helpers import catalog_xml, commodity_xml
from tires_sync.webmim import parse_unload


def test_commercial_positions_never_enter_the_offers_or_the_issues():
    xml = catalog_xml(
        commodity_xml(NNOMMODIF="1", SCARTYPE=""), commodity_xml(NNOMMODIF="2", SCARTYPE="C"), commodity_xml(NNOMMODIF="3", SCARTYPE="lt"),
        commodity_xml(NNOMMODIF="4", SCARTYPE="C", NREST="не число"))                      # not even the broken ones are reported
    parsed = parse_unload(xml, "T1")
    assert set(parsed.offers) == {"1"} and parsed.issues == {} and parsed.commercial_skipped == 3


def test_a_published_commercial_position_is_taken_off_as_gone_from_the_supplier(tmp_path):
    from datetime import date
    from helpers import settings_dict
    from tires_sync import connections, cycle, store
    from tires_sync.catalog import parse_reference
    from tires_sync.settings_store import init_settings
    ref = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(guard_min=10), "2026-10-14T12:00:00")
    connections.create_account(conn, "A", "2026-10-14T12:00:00", "o")
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="1"), commodity_xml(NNOMMODIF="2")), ref, date(2026, 10, 14), tmp_path / "feed")
    from tires_sync import publishing
    connections.set_live(conn, 1, True, "2026-10-14T12:00:00", "o")
    conn.commit()
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    results = cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="1"), commodity_xml(NNOMMODIF="2", SCARTYPE="C")), ref,
                                     date(2026, 10, 15), tmp_path / "feed")
    assert results[0].ok and results[0].counts.get("remove") == 1
