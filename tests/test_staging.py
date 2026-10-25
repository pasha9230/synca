from __future__ import annotations

import pytest

from helpers import settings_dict
from tires_sync import staging, store
from tires_sync.settings_store import StaleDraftError, confirm_draft, get_active, init_settings

T = "2026-10-07T10:00:00"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    init_settings(c, settings_dict(), T)
    return c


def test_nothing_staged_at_first(conn):
    assert staging.summary(conn) == {"draft_id": None, "count": 0, "stale": False, "changes": {}}


def test_position_rules_accumulate_in_one_working_draft_and_last_action_wins(conn):
    staging.set_override(conn, "100", "off", T)
    staging.set_override(conn, "101", "on", T)
    first = staging.summary(conn)
    assert first["count"] == 2 and first["changes"]["overrides"] == {"1:100": "off", "1:101": "on"}
    staging.set_override(conn, "100", "on", T)
    staging.set_override(conn, "101", None, T)
    again = staging.summary(conn)
    assert again["changes"]["overrides"] == {"1:100": "on"} and again["count"] == 1
    assert again["draft_id"] == first["draft_id"]


def test_returning_to_the_active_state_empties_the_draft(conn):
    staging.set_override(conn, "100", "off", T)
    staging.set_override(conn, "100", None, T)
    assert staging.summary(conn)["count"] == 0


def test_brand_season_switch_and_percents(conn):
    staging.set_brand_state(conn, [("KUMHO", "winter", 3.0, 3.0, True)], T)
    s = staging.summary(conn)
    assert ["KUMHO", "winter"] in s["changes"]["enabled"]
    assert s["changes"]["pricing"]["KUMHO|winter"] == {"pct_small": 3.0, "pct_large": 3.0}
    staging.set_brand_state(conn, [("KUMHO", "winter", 3.0, 3.0, False)], T)
    assert ["KUMHO", "winter"] not in staging.summary(conn)["changes"].get("enabled", [])


def test_whole_brand_off_and_on_touch_only_that_brand(conn):
    staging.set_brand_enabled(conn, "IKON TYRES", False, T)
    assert staging.summary(conn)["changes"]["enabled"] == [["PIRELLI", "winter"]]
    staging.set_brand_enabled(conn, "IKON TYRES", True, T)
    assert staging.summary(conn)["count"] == 0


def test_a_percent_needs_both_values_and_must_be_a_number(conn):
    with pytest.raises(ValueError):
        staging.set_brand_state(conn, [("KUMHO", "winter", None, 3.0, True)], T)
    with pytest.raises(ValueError):
        staging.set_brand_state(conn, [("KUMHO", "winter", float("nan"), 3.0, True)], T)
    assert staging.summary(conn)["count"] == 0


def test_applying_the_working_draft_makes_a_new_version_and_clears_it(conn):
    staging.set_override(conn, "100", "off", T)
    d = staging.summary(conn)["draft_id"]
    confirm_draft(conn, d, T, "owner")
    assert get_active(conn)[1]["overrides"] == {"1:100": "off"}
    assert staging.summary(conn)["draft_id"] is None


def test_a_stale_working_draft_is_reported_and_blocks_new_staging_until_cancelled(conn):
    staging.set_override(conn, "100", "off", T)
    from tires_sync.settings_store import create_draft
    other = create_draft(conn, {"min_stock": 6}, T)
    confirm_draft(conn, other, T, "owner")
    assert staging.summary(conn)["stale"] is True
    with pytest.raises(StaleDraftError):
        staging.set_override(conn, "101", "off", T)
    staging.cancel_all(conn, T, "owner")
    staging.set_override(conn, "101", "off", T)
    assert staging.summary(conn)["count"] == 1 and staging.summary(conn)["stale"] is False


def test_cancel_all_discards_everything(conn):
    staging.set_override(conn, "100", "off", T)
    staging.cancel_all(conn, T, "owner")
    assert staging.summary(conn)["count"] == 0
