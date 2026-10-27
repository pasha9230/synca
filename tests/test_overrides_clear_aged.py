from __future__ import annotations

import json

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, staging, store
from tires_sync.candidates import evaluate
from tires_sync.cli import main
from tires_sync.settings_store import active_settings, get_active, init_settings, list_versions

T = "2026-10-27T10:00:00"


def _setup(tmp_path, overrides):
    """100: aged by name, on; 101: aged by year, on; 102: normal, on; 103: aged, off; 104: aged, no rule; 105: normal, off."""
    xml = catalog_xml(
        commodity_xml(NNOMMODIF="100", SMODIFNAME="IKON 205/55/16 T 94 Test старше 3-х лет", NREST="6"),
        commodity_xml(NNOMMODIF="101", SMNFYEAR="старше 5-ти лет", NREST="2"),
        commodity_xml(NNOMMODIF="102"),
        commodity_xml(NNOMMODIF="103", SMODIFNAME="IKON 205/55/16 T 94 Test старше 3-х лет"),
        commodity_xml(NNOMMODIF="104", SMODIFNAME="IKON 205/55/16 T 94 Test старше 3-х лет"),
        commodity_xml(NNOMMODIF="105"),
    )
    (tmp_path / "unload.xml").write_bytes(xml)
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(overrides=overrides), T)
    conn.close()
    return ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml")]


ON_OFF = {"1:100": "on", "1:101": "on", "1:102": "on", "1:103": "off", "1:105": "off"}


def _overrides(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    try:
        return get_active(conn)[1]["overrides"]
    finally:
        conn.close()


def _versions(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    try:
        return list_versions(conn)
    finally:
        conn.close()


def test_dry_run_lists_and_writes_nothing(tmp_path, capsys):
    args = _setup(tmp_path, ON_OFF)
    before = _versions(tmp_path)
    assert main(["overrides-clear-aged"] + args) == 0
    out = capsys.readouterr().out
    assert "100" in out and "101" in out and "Всего: 2" in out
    assert "102 |" not in out and "103 |" not in out and "105 |" not in out      # only aged «on»
    assert "ничего не записано" in out.lower()
    assert _versions(tmp_path) == before and _overrides(tmp_path) == ON_OFF


def test_dry_run_shows_stock_and_whether_it_is_in_the_feed_now(tmp_path, capsys):
    args = _setup(tmp_path, ON_OFF)
    conn = store.connect(tmp_path / "t.db")
    acc = connections.create_account(conn, "Основной", T, "test")
    conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, location, first_hold, snapshot) "
                 "VALUES (?, '100', 5000, 5000, 'P1', 'x', NULL, '')", (acc,))
    conn.commit()
    conn.close()
    main(["overrides-clear-aged"] + args)
    lines = {l.split("|")[0].strip(): l for l in capsys.readouterr().out.splitlines() if "|" in l}
    assert "6" in lines["100"].split("|")[2] and "в фиде" in lines["100"] and "не в фиде" not in lines["100"]
    assert "не в фиде" in lines["101"]


def test_apply_removes_only_aged_on_overrides(tmp_path, capsys):
    args = _setup(tmp_path, ON_OFF)
    assert main(["overrides-clear-aged"] + args + ["--apply"]) == 0
    # the aged «on» are gone (not turned «off»); the non-aged «on» and every «off» stay as they were
    assert _overrides(tmp_path) == {"1:102": "on", "1:103": "off", "1:105": "off"}


def test_apply_makes_a_new_version_by_cli_with_a_note_and_an_audit_row(tmp_path):
    args = _setup(tmp_path, ON_OFF)
    main(["overrides-clear-aged"] + args + ["--apply"])
    versions = _versions(tmp_path)
    assert len(versions) == 2
    top = versions[0]
    assert top["active"] and top["author"] == "cli" and top["note"] == "снято ручное вкл у позиций «старше» (2)"
    conn = store.connect(tmp_path / "t.db")
    rows = conn.execute("SELECT author, action, detail FROM audit WHERE action = 'settings_confirm'").fetchall()
    assert rows and rows[-1][0] == "cli"
    assert json.loads(rows[-1][2])["changes"]["overrides"] == {"1:102": "on", "1:103": "off", "1:105": "off"}
    # no other settings changed
    old = json.loads(conn.execute("SELECT json FROM settings_version WHERE id = ?", (versions[1]["id"],)).fetchone()[0])
    new = json.loads(conn.execute("SELECT json FROM settings_version WHERE id = ?", (top["id"],)).fetchone()[0])
    old.pop("overrides"), new.pop("overrides")
    assert old == new


def test_after_apply_the_aged_positions_are_excluded_by_the_rule(tmp_path):
    from tires_sync.webmim import parse_unload
    args = _setup(tmp_path, ON_OFF)
    offers = parse_unload((tmp_path / "unload.xml").read_bytes(), "T1").offers
    ref = {}
    conn = store.connect(tmp_path / "t.db")
    before = evaluate(offers, {}, active_settings(conn), ref, {})
    assert "100" in before.forced and before.excluded.get("100") != "old_production"
    main(["overrides-clear-aged"] + args + ["--apply"])
    after = evaluate(offers, {}, active_settings(conn), ref, {})
    assert after.excluded["100"] == "old_production" and after.excluded["101"] == "old_production"
    assert "100" not in after.forced and "101" not in after.forced
    assert after.excluded["103"] == "manual_off"          # an «off» stays an «off»
    assert "102" in after.forced                          # a normal «on» keeps working


def test_nothing_to_clear_changes_nothing(tmp_path, capsys):
    args = _setup(tmp_path, {"1:102": "on", "1:103": "off"})
    assert main(["overrides-clear-aged"] + args + ["--apply"]) == 0
    assert "Всего: 0" in capsys.readouterr().out
    assert len(_versions(tmp_path)) == 1


def test_apply_refuses_when_the_panel_has_an_unconfirmed_draft(tmp_path, capsys):
    args = _setup(tmp_path, ON_OFF)
    conn = store.connect(tmp_path / "t.db")
    staging.set_override(conn, "105", None, T)
    conn.close()
    assert main(["overrides-clear-aged"] + args + ["--apply"]) == 2
    assert "черновик" in capsys.readouterr().err
    assert len(_versions(tmp_path)) == 1 and _overrides(tmp_path) == ON_OFF


def test_apply_refuses_if_the_active_version_changed_after_the_list_was_made(tmp_path, monkeypatch):
    from tires_sync import overrides_clear
    args = _setup(tmp_path, ON_OFF)
    real = overrides_clear.find_aged_on

    # bump the active version between the list and the write
    def bump(*a, **k):
        found = real(*a, **k)
        other = store.connect(tmp_path / "t.db")
        raw = get_active(other)[1]
        raw = dict(raw, min_stock=5)
        raw.pop("version", None)
        other.execute("INSERT INTO settings_version (created, author, note, json) VALUES (?,?,?,?)",
                      (T, "x", "race", json.dumps(raw)))
        other.execute("UPDATE settings_active SET version_id = (SELECT MAX(id) FROM settings_version)")
        other.commit()
        other.close()
        return found

    monkeypatch.setattr(overrides_clear, "find_aged_on", bump)
    assert main(["overrides-clear-aged"] + args + ["--apply"]) == 2
    assert _overrides(tmp_path) == ON_OFF


def test_a_version_made_between_the_read_and_the_draft_is_not_overwritten(tmp_path, monkeypatch, capsys):
    """The panel confirms a new «off» right after apply_clear read the settings: its overrides must not be lost."""
    from tires_sync import overrides_clear
    args = _setup(tmp_path, ON_OFF)
    real_create = overrides_clear.create_draft

    def create_after_a_panel_change(conn, changes, now):
        other = store.connect(tmp_path / "t.db")
        raw = get_active(other)[1]
        raw.pop("version", None)
        raw["overrides"] = dict(raw["overrides"], **{"1:102": "off"})
        other.execute("INSERT INTO settings_version (created, author, note, json) VALUES (?,?,?,?)", (T, "panel", "x", json.dumps(raw)))
        other.execute("UPDATE settings_active SET version_id = (SELECT MAX(id) FROM settings_version)")
        other.commit()
        other.close()
        return real_create(conn, changes, now)

    monkeypatch.setattr(overrides_clear, "create_draft", create_after_a_panel_change)
    assert main(["overrides-clear-aged"] + args + ["--apply"]) == 2
    assert "изменились" in capsys.readouterr().err
    assert _overrides(tmp_path) == dict(ON_OFF, **{"1:102": "off"})
    conn = store.connect(tmp_path / "t.db")
    assert conn.execute("SELECT COUNT(*) FROM draft WHERE status = 'open'").fetchone()[0] == 0


def test_a_panel_draft_stays_untouched_on_refusal(tmp_path):
    args = _setup(tmp_path, ON_OFF)
    conn = store.connect(tmp_path / "t.db")
    staging.set_override(conn, "105", None, T)
    before = staging.summary(conn)
    assert main(["overrides-clear-aged"] + args + ["--apply"]) == 2
    assert staging.summary(conn) == before and len(_versions(tmp_path)) == 1


def test_second_supplier_aged_position_is_cleared(tmp_path, capsys):
    args = _setup(tmp_path, dict(ON_OFF, **{"1:s2-300": "on"}))
    conn = store.connect(tmp_path / "t.db")
    two = connections.create_supplier(conn, "Второй", T, "test")
    connections.set_config(conn, "webmim", connections.supplier_owner(two),
                           {"base_url": "https://b.example", "mode": "apikey", "territory_rn": "T2"}, T, "test")
    conn.close()
    (tmp_path / "unload-2.xml").write_bytes(catalog_xml(
        commodity_xml(NNOMMODIF="300", TERRITORY_RN="T2", SMODIFNAME="IKON 205/55/16 T 94 Test старше 3-х лет")))
    assert main(["overrides-clear-aged"] + args) == 0
    out = capsys.readouterr().out
    assert "s2-300 |" in out and "Всего: 3" in out
    assert main(["overrides-clear-aged"] + args + ["--apply"]) == 0
    assert "1:s2-300" not in _overrides(tmp_path)


def _with_unconfigured_supplier(tmp_path, overrides):
    args = _setup(tmp_path, overrides)
    conn = store.connect(tmp_path / "t.db")
    connections.create_supplier(conn, "Второй", T, "test")        # no unload file for it
    conn.close()
    return args


def test_an_unavailable_supplier_with_a_manual_on_blocks_apply(tmp_path, capsys):
    args = _with_unconfigured_supplier(tmp_path, dict(ON_OFF, **{"1:s2-300": "on"}))
    assert main(["overrides-clear-aged"] + args + ["--apply"]) == 2
    out = capsys.readouterr()
    assert "ВНИМАНИЕ" in out.out and "неполный" in out.err
    assert _overrides(tmp_path) == dict(ON_OFF, **{"1:s2-300": "on"}) and len(_versions(tmp_path)) == 1


def test_an_unavailable_supplier_without_a_manual_on_does_not_block_apply(tmp_path):
    args = _with_unconfigured_supplier(tmp_path, dict(ON_OFF, **{"1:s2-300": "off"}))
    assert main(["overrides-clear-aged"] + args + ["--apply"]) == 0
    assert _overrides(tmp_path) == {"1:102": "on", "1:103": "off", "1:105": "off", "1:s2-300": "off"}


def test_a_panel_draft_made_after_the_first_check_is_not_lost(tmp_path, monkeypatch, capsys):
    from tires_sync import overrides_clear
    args = _setup(tmp_path, ON_OFF)
    real_create = overrides_clear.create_draft

    def create_after_a_panel_click(conn, changes, now):
        other = store.connect(tmp_path / "t.db")
        staging.set_override(other, "105", None, T)
        other.close()
        return real_create(conn, changes, now)

    monkeypatch.setattr(overrides_clear, "create_draft", create_after_a_panel_click)
    assert main(["overrides-clear-aged"] + args + ["--apply"]) == 2
    assert "черновик" in capsys.readouterr().err
    assert len(_versions(tmp_path)) == 1 and _overrides(tmp_path) == ON_OFF
    conn = store.connect(tmp_path / "t.db")
    assert staging.summary(conn)["count"] == 1
