import pytest

from tires_sync import store
from tires_sync.settings_store import (
    PercentLimitError, SettingsNotInitialized, StaleDraftError, active_settings, cancel_draft, confirm_draft,
    create_draft, get_active, init_settings, rollback,
)
from helpers import settings_dict

T1 = "2026-10-07T10:00:00"
T2 = "2026-10-07T11:00:00"


def fresh(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), T1)
    return conn


def test_init_creates_version_one_and_rejects_second_init(tmp_path):
    conn = fresh(tmp_path)
    assert active_settings(conn).version == 1
    with pytest.raises(ValueError):
        init_settings(conn, settings_dict(), T2)


def test_reading_before_init_raises_clear_error(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    with pytest.raises(SettingsNotInitialized):
        get_active(conn)


def test_confirm_draft_creates_new_version_and_audits(tmp_path):
    conn = fresh(tmp_path)
    draft = create_draft(conn, {"min_stock": 6}, T2)
    new_id = confirm_draft(conn, draft, T2, "owner")
    assert new_id == 2 and active_settings(conn).min_stock == 6
    assert store.list_audit(conn)[0]["action"] == "settings_confirm"


def test_second_draft_from_same_base_is_stale_after_first_confirm(tmp_path):
    conn = fresh(tmp_path)
    a = create_draft(conn, {"min_stock": 6}, T2)
    b = create_draft(conn, {"min_diameter": 17}, T2)
    confirm_draft(conn, a, T2, "owner")
    with pytest.raises(StaleDraftError):
        confirm_draft(conn, b, T2, "owner")
    assert active_settings(conn).min_diameter == 15


def test_draft_cannot_be_confirmed_twice_or_after_cancel(tmp_path):
    conn = fresh(tmp_path)
    a = create_draft(conn, {"min_stock": 6}, T2)
    confirm_draft(conn, a, T2, "owner")
    with pytest.raises(ValueError):
        confirm_draft(conn, a, T2, "owner")
    b = create_draft(conn, {"min_stock": 7}, T2)
    cancel_draft(conn, b, T2, "owner")
    with pytest.raises(ValueError):
        confirm_draft(conn, b, T2, "owner")
    assert active_settings(conn).min_stock == 6


def test_invalid_structure_is_rejected_at_draft_time_and_never_activates(tmp_path):
    conn = fresh(tmp_path)
    with pytest.raises(ValueError):
        create_draft(conn, {"enabled": [["KUMHO", "winter"]]}, T2)
    with pytest.raises(ValueError):
        create_draft(conn, {"point_values": {"P1": 50, "P2": 40}}, T2)
    assert get_active(conn)[0] == 1


def test_percent_outside_limit_needs_strong_confirmation(tmp_path):
    conn = fresh(tmp_path)
    pricing = settings_dict()["pricing"]
    pricing["IKON TYRES|winter"]["pct_small"] = -20
    draft = create_draft(conn, {"pricing": pricing}, T2)
    with pytest.raises(PercentLimitError) as err:
        confirm_draft(conn, draft, T2, "owner")
    assert err.value.max_pct == 20
    assert get_active(conn)[0] == 1
    confirm_draft(conn, draft, T2, "owner", strong=True)
    assert get_active(conn)[0] == 2


def test_rollback_creates_new_version_with_old_values(tmp_path):
    conn = fresh(tmp_path)
    confirm_draft(conn, create_draft(conn, {"min_stock": 9}, T2), T2, "owner")
    new_id = rollback(conn, 1, T2, "owner")
    assert new_id == 3 and active_settings(conn).min_stock == 4
    assert store.list_audit(conn)[0]["action"] == "settings_rollback"


def test_concurrent_confirmations_cannot_both_win(tmp_path, monkeypatch):
    import sqlite3
    from tires_sync import settings_store
    path = tmp_path / "t.db"
    a = store.connect(path, timeout=0.2)
    b = store.connect(path, timeout=0.2)
    init_settings(a, settings_dict(), T1)
    da = create_draft(a, {"min_stock": 11}, T2)
    db = create_draft(a, {"min_stock": 12}, T2)
    real = settings_store._insert_version
    fired = {"done": False}

    def racing(conn, *args, **kw):
        if conn is a and not fired["done"]:
            fired["done"] = True
            try:
                confirm_draft(b, db, T2, "owner")
            except (sqlite3.OperationalError, StaleDraftError):
                pass
        return real(conn, *args, **kw)

    monkeypatch.setattr(settings_store, "_insert_version", racing)
    confirm_draft(a, da, T2, "owner")
    statuses = [r[0] for r in a.execute("SELECT status FROM draft ORDER BY id")]
    assert statuses.count("confirmed") == 1
    assert active_settings(a).min_stock == 11


def test_draft_cannot_raise_pct_limit_to_bypass_strong_confirmation(tmp_path):
    conn = fresh(tmp_path)
    pricing = settings_dict()["pricing"]
    pricing["IKON TYRES|winter"]["pct_small"] = -80
    draft = create_draft(conn, {"pricing": pricing, "pct_limit": 100}, T2)
    with pytest.raises(PercentLimitError):
        confirm_draft(conn, draft, T2, "owner")
    assert get_active(conn)[0] == 1


def test_raising_pct_limit_alone_requires_strong_confirmation(tmp_path):
    conn = fresh(tmp_path)
    draft = create_draft(conn, {"pct_limit": 50.0}, T2)
    with pytest.raises(PercentLimitError):
        confirm_draft(conn, draft, T2, "owner")
    confirm_draft(conn, draft, T2, "owner", strong=True)
    assert active_settings(conn).pct_limit == 50.0


def test_unrelated_draft_after_a_strong_confirmation_does_not_need_strong(tmp_path):
    conn = fresh(tmp_path)
    pricing = settings_dict()["pricing"]
    pricing["IKON TYRES|winter"]["pct_small"] = -20
    confirm_draft(conn, create_draft(conn, {"pricing": pricing}, T2), T2, "owner", strong=True)
    confirm_draft(conn, create_draft(conn, {"min_stock": 7}, T2), T2, "owner")
    assert active_settings(conn).min_stock == 7


def test_unknown_or_forbidden_keys_in_changes_are_rejected(tmp_path):
    conn = fresh(tmp_path)
    with pytest.raises(ValueError, match="keys"):
        create_draft(conn, {"junk": 1}, T2)
    with pytest.raises(ValueError, match="keys"):
        create_draft(conn, {"version": 999}, T2)


from tires_sync.settings_store import get_draft, needs_strong_confirmation


def test_get_draft_returns_changes_and_status(tmp_path):
    conn = fresh(tmp_path)
    draft = create_draft(conn, {"min_stock": 6}, T2)
    assert get_draft(conn, draft) == {"id": draft, "base_version": 1, "changes": {"min_stock": 6}, "status": "open"}
    with pytest.raises(ValueError):
        get_draft(conn, 99)


def test_needs_strong_confirmation_reports_out_of_range_and_limit_changes():
    raw = settings_dict()
    pricing = settings_dict()["pricing"]
    pricing["IKON TYRES|winter"]["pct_small"] = -20
    assert needs_strong_confirmation(raw, {"pricing": pricing}) is True
    assert needs_strong_confirmation(raw, {"pct_limit": 50.0}) is True
    assert needs_strong_confirmation(raw, {"min_stock": 7}) is False
