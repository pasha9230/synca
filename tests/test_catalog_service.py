from __future__ import annotations

from helpers import catalog_xml, commodity_xml, settings_dict
from panel_helpers import REF_TEXT, login, make_app
from tires_sync import staging, store
from tires_sync.catalog import parse_reference
from tires_sync.config import settings_from_dict
from tires_sync.panel import service
from tires_sync.settings_store import get_active, init_settings
from tires_sync.webmim import parse_unload

T = "2026-10-07T10:00:00"
ROWS = (
    commodity_xml(NNOMMODIF="1", NREST="12", SMNFCODE="C1"),
    commodity_xml(NNOMMODIF="2", NREST="2", SMNFCODE="C2"),
    commodity_xml(NNOMMODIF="3", SDIAMETR="13", SMNFCODE="C3", NREST="9"),
)


def setup(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), T)
    parsed = parse_unload(catalog_xml(*ROWS), "T1")
    return conn, parsed, parse_reference(REF_TEXT)


def groups(conn, parsed, ref):
    raw = get_active(conn)[1]
    changes = staging.summary(conn)["changes"]
    settings = settings_from_dict({**raw, **changes}, enforce_pct_limit=False)
    return service.catalog_groups(conn, settings, parsed, ref, base_raw=raw)


def items(gs):
    return {i["goods_id"]: i for g in gs for i in g["items"]}


def test_brand_header_carries_season_switches_and_percents(tmp_path):
    conn, parsed, ref = setup(tmp_path)
    ikon = next(g for g in groups(conn, parsed, ref) if g["brand"] == "IKON TYRES")
    winter = ikon["seasons"]["winter"]
    assert winter["enabled"] is True and winter["small"] == -3.82 and winter["large"] == -4.28 and winter["count"] == 3
    assert ikon["seasons"]["summer"]["enabled"] is False and ikon["seasons"]["summer"]["count"] == 0


def test_manual_off_and_on_are_labelled_and_pending_until_confirmed(tmp_path):
    conn, parsed, ref = setup(tmp_path)
    staging.set_override(conn, "1", "off", T)
    staging.set_override(conn, "3", "on", T)
    got = items(groups(conn, parsed, ref))
    assert got["1"]["state"] == "no" and got["1"]["reason"] == "снято вами" and got["1"]["manual"] == "off" and got["1"]["pending"]
    assert got["3"]["state"] == "feed" and got["3"]["manual"] == "on" and got["3"]["pending"]
    assert got["3"]["reason"] == "добавлено вручную"
    assert got["2"]["manual"] is None and got["2"]["pending"] is False


def test_staged_brand_switch_changes_the_pending_flag_of_the_season_only(tmp_path):
    conn, parsed, ref = setup(tmp_path)
    staging.set_brand_state(conn, [("IKON TYRES", "winter", -3.82, -4.28, False)], T)
    ikon = next(g for g in groups(conn, parsed, ref) if g["brand"] == "IKON TYRES")
    assert ikon["seasons"]["winter"]["enabled"] is False and ikon["seasons"]["winter"]["pending"] is True
    assert ikon["seasons"]["summer"]["pending"] is False


def test_manual_rule_for_a_position_the_supplier_no_longer_lists_is_reported(tmp_path):
    conn, parsed, ref = setup(tmp_path)
    staging.set_override(conn, "999", "on", T)
    raw = get_active(conn)[1]
    settings = settings_from_dict({**raw, **staging.summary(conn)["changes"]}, enforce_pct_limit=False)
    orphans = service.orphan_overrides(settings, parsed)
    assert orphans == [{"goods_id": "999", "mode": "on"}]
    assert service.orphan_overrides(settings_from_dict(raw), parsed) == []


def test_models_overview_groups_goods_and_stock_per_brand(tmp_path):
    app = make_app(tmp_path)
    login(app.test_client())
    from tires_sync.panel.data import load_parsed, load_reference
    from tires_sync.settings_store import get_active
    conn = store.connect(app.config["DB_PATH"])
    rows = service.brands_overview(conn, get_active(conn)[1], load_parsed(app.config["UNLOAD_PATH"], "T1"),
                                   load_reference(app.config["REFERENCE_PATH"]))
    ikon = next(r for r in rows if r["brand_key"] == "IKON TYRES")
    names = {m["name"]: m for m in ikon["models"]}
    assert set(names) == {"Test Model", "Test Model 2"}
    assert names["Test Model"]["goods"] == 1 and names["Test Model"]["stock"] == 10 and names["Test Model"]["avito_model"] == "Test Model"
