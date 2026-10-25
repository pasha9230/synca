"""A position that is gone from the supplier's unload (or below the stock threshold) is taken off at once (owner's decision, 2026-10-14)."""
from datetime import date, datetime

from helpers import make_offer, make_settings
from tires_sync.candidates import Candidate, Evaluation
from tires_sync.decide import WAIT_FOR_MORNING_REFILL, decide
from tires_sync.models import Action, PublishedItem

TODAY = date(2026, 10, 14)
NOON = datetime(2026, 10, 14, 12, 11)
S = make_settings()


def pub(gid="100", location="supplier"):
    return PublishedItem(gid, 4810, 5000.0, "P1", location, None)


def one(ds, gid="100"):
    return [d for d in ds.decisions if d.goods_id == gid][0]


def test_the_morning_wait_is_off_by_default():
    assert WAIT_FOR_MORNING_REFILL is False


def test_a_position_that_left_the_unload_is_removed_in_the_same_cycle_even_with_the_clock_given():
    published = {str(n): pub(str(n)) for n in range(100, 140)}
    ev = Evaluation()
    ev.ok = {str(n): Candidate(str(n), make_offer(goods_id=str(n), stock=10), 4810, "Ikon Tyres", "Test Model") for n in range(101, 140)}
    d = one(decide(ev, published, set(), {}, S, TODAY, now=NOON), "100")
    assert (d.action, d.reason) == (Action.REMOVE, "absent_from_supplier")


def test_a_position_below_the_stock_threshold_is_removed_at_once_and_stays_suppressed_until_it_recovers():
    cand = Candidate("100", make_offer(goods_id="100", stock=1), 4810, "Ikon Tyres", "Test Model")
    ev = Evaluation()
    ev.ok = {"100": cand}
    published = {str(n): pub(str(n)) for n in range(100, 140)}
    ev.ok.update({str(n): Candidate(str(n), make_offer(goods_id=str(n), stock=10), 4810, "Ikon Tyres", "Test Model") for n in range(101, 140)})
    d = one(decide(ev, published, set(), {}, S, TODAY, now=NOON), "100")
    assert (d.action, d.reason, d.suppressed) == (Action.REMOVE, "low_stock", True)


def test_the_mass_removal_guard_still_holds_back_a_collapse_of_the_unload():
    published = {str(n): pub(str(n)) for n in range(100, 140)}
    ds = decide(Evaluation(), published, set(), {}, S, TODAY, now=NOON)        # the whole unload is gone: a broken file, not 40 sold-out positions
    assert ds.guard_tripped and all(d.action is Action.HOLD and d.reason.startswith("guard:") for d in ds.decisions)


def test_a_position_on_the_owner_own_point_is_never_touched():
    ds = decide(Evaluation(), {"100": pub(location="own")}, set(), {}, S, TODAY, now=NOON)
    assert one(ds).action is Action.KEEP
