"""Other sellers of the supplier put goods into baskets during the day and the goods leave the unload; the baskets are emptied at midnight
and the catalog fills up again by the morning cycle. A position that left during the day waits for the morning (owner's decision, 2026-10-12)."""
from datetime import date, datetime, timedelta

import pytest

from helpers import make_offer, make_settings
from tires_sync import store
from tires_sync.candidates import Candidate, Evaluation
from tires_sync.decide import cart_release_at, decide
from tires_sync.models import Action, PublishedItem

pytestmark = pytest.mark.usefixtures("morning_wait")  # these tests describe the wait for the morning refill, which is switched off by default

TODAY = date(2026, 10, 12)
S = make_settings()


def cand(gid="100", stock=10, base=5000.0, price=4810):
    return Candidate(gid, make_offer(goods_id=gid, stock=stock, base_price=base), price, "Ikon Tyres", "Test Model")


def pub(gid="100", first_hold=None, location="supplier"):
    return PublishedItem(gid, 4810, 5000.0, "P1", location, first_hold)


def one(ds, gid="100"):
    return [d for d in ds.decisions if d.goods_id == gid][0]


def run(ev, now, published=None):
    return decide(ev, published or {}, set(), {}, S, TODAY, now=now)


@pytest.mark.parametrize("since,release", [
    (datetime(2026, 10, 12, 12, 11), datetime(2026, 10, 13, 6, 30)),    # left at midday: the next morning
    (datetime(2026, 10, 12, 18, 11), datetime(2026, 10, 13, 6, 30)),
    (datetime(2026, 10, 12, 0, 11), datetime(2026, 10, 12, 6, 30)),     # left just after midnight: this morning is enough
    (datetime(2026, 10, 12, 6, 29), datetime(2026, 10, 12, 6, 30)),
    (datetime(2026, 10, 12, 6, 11), datetime(2026, 10, 12, 6, 30)),     # the 06:10 cycle is before the release: it still waits
    (datetime(2026, 10, 12, 6, 30), datetime(2026, 10, 13, 6, 30)),     # at the release moment itself: wait a day
    (datetime(2026, 10, 12, 6, 45), datetime(2026, 10, 13, 6, 30)),
    (datetime(2026, 12, 31, 20, 0), datetime(2027, 1, 1, 6, 30)),
])
def test_release_is_the_first_morning_after_the_position_left(since, release):
    assert cart_release_at(since) == release


def test_a_position_that_left_the_unload_is_kept_until_the_morning_then_taken_off():
    noon = datetime(2026, 10, 12, 12, 11)
    d = one(run(Evaluation(), noon, {"100": pub()}))
    assert (d.action, d.reason, d.first_hold, d.price) == (Action.HOLD, "cart_wait:absent_from_supplier", noon, 4810)
    later = datetime(2026, 10, 13, 0, 11)                                # the 00:11 cycle: the catalog is not refilled yet
    assert one(run(Evaluation(), later, {"100": pub(first_hold=noon)})).action is Action.HOLD
    early = datetime(2026, 10, 13, 6, 11)                                # the 06:10 cycle is before the release: the position still waits
    assert one(run(Evaluation(), early, {"100": pub(first_hold=noon)})).action is Action.HOLD
    morning = datetime(2026, 10, 13, 6, 45)                              # the last check before Avito reads at 07:00: still gone, so it goes
    d = one(run(Evaluation(), morning, {"100": pub(first_hold=noon)}))
    assert (d.action, d.reason) == (Action.REMOVE, "absent_from_supplier")


def test_a_position_whose_stock_fell_below_the_threshold_waits_too_and_keeps_the_suppression_when_it_goes():
    noon = datetime(2026, 10, 12, 12, 11)
    d = one(run(Evaluation(ok={"100": cand(stock=3)}), noon, {"100": pub()}))
    assert (d.action, d.reason) == (Action.HOLD, "cart_wait:low_stock")
    d = one(run(Evaluation(ok={"100": cand(stock=3)}), datetime(2026, 10, 13, 6, 45), {"100": pub(first_hold=noon)}))
    assert (d.action, d.reason, d.suppressed) == (Action.REMOVE, "low_stock", True)


def test_a_position_that_comes_back_is_kept_again_and_the_wait_is_forgotten():
    noon = datetime(2026, 10, 12, 12, 11)
    d = one(run(Evaluation(ok={"100": cand(stock=10)}), datetime(2026, 10, 13, 6, 45), {"100": pub(first_hold=noon)}))
    assert d.action is Action.KEEP and d.first_hold is None


def test_a_position_that_is_off_by_the_owners_settings_is_not_waiting():
    for reason in ("not_enabled", "manual_off", "brand_excluded", "size_excluded", "diameter"):
        d = one(run(Evaluation(excluded={"100": reason}), datetime(2026, 10, 12, 12, 11), {"100": pub()}))
        assert (d.action, d.reason) == (Action.REMOVE, reason)


def test_a_hold_written_without_the_time_is_taken_to_have_begun_at_noon():
    legacy = date(2026, 10, 12)
    assert one(run(Evaluation(), datetime(2026, 10, 12, 18, 11), {"100": pub(first_hold=legacy)})).action is Action.HOLD
    assert one(run(Evaluation(), datetime(2026, 10, 13, 6, 45), {"100": pub(first_hold=legacy)})).action is Action.REMOVE


def test_without_a_clock_positions_go_at_once_as_before():
    d = one(decide(Evaluation(), {"100": pub()}, set(), {}, S, TODAY))
    assert (d.action, d.reason) == (Action.REMOVE, "absent_from_supplier")


def test_a_position_on_the_owners_own_point_is_not_touched():
    d = one(run(Evaluation(), datetime(2026, 10, 12, 12, 11), {"100": pub(location="own")}))
    assert d.action is Action.KEEP


def test_the_mass_removal_guard_counts_only_real_removals_and_still_stops_a_morning_collapse():
    ids = [str(i) for i in range(100, 130)]
    published = {g: pub(g) for g in ids}
    ev = Evaluation(ok={ids[0]: cand(ids[0])})
    noon = datetime(2026, 10, 12, 12, 11)
    ds = run(ev, noon, published)
    assert not ds.guard_tripped and sum(1 for d in ds.decisions if d.action is Action.HOLD) == 29       # all wait, no alarm
    held = {g: pub(g, first_hold=noon) for g in ids}
    ds = run(ev, datetime(2026, 10, 13, 6, 45), held)                                                   # still gone after the refill
    assert ds.guard_tripped and sum(1 for d in ds.decisions if d.action is Action.HOLD) == 29


def test_the_time_of_the_wait_survives_the_database(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    noon = datetime(2026, 10, 12, 12, 11, 5)
    conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, location, first_hold, snapshot) VALUES (1,'a',1,1,'p','supplier',?, '<Ad/>')",
                 (noon.isoformat(),))
    conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, location, first_hold, snapshot) VALUES (1,'b',1,1,'p','supplier','2026-10-11', '<Ad/>')")
    conn.commit()
    loaded = store.load_published(conn, 1)
    assert loaded["a"].first_hold == noon and loaded["b"].first_hold == date(2026, 10, 11)


def test_the_timer_runs_every_hour_and_keeps_the_last_morning_check_at_0645():
    from pathlib import Path
    timer = (Path(__file__).resolve().parent.parent / "deploy" / "tires-cycle.timer").read_text()
    lines = [l for l in timer.splitlines() if l.startswith("OnCalendar=")]
    assert any("*:40:00" in l for l in lines) and any("06:45:00" in l for l in lines)    # the owner, 2026-10-20: every hour
