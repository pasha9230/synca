from datetime import date, timedelta

from tires_sync.candidates import Candidate, Evaluation
from tires_sync.decide import decide
from tires_sync.models import Action, PublishedItem
from helpers import make_offer, make_settings

TODAY = date(2026, 10, 6)
S = make_settings()


def cand(gid="100", stock=10, base=5000.0, price=4810):
    return Candidate(gid, make_offer(goods_id=gid, stock=stock, base_price=base), price, "Ikon Tyres", "Test Model")


def pub(gid="100", price=4810, base=5000.0, point="P1", location="supplier", first_hold=None):
    return PublishedItem(gid, price, base, point, location, first_hold)


def one(ds, gid="100"):
    return [d for d in ds.decisions if d.goods_id == gid][0]


def run(ev, published=None, suppressed=None, known=None, settings=S):
    return decide(ev, published or {}, suppressed or set(), known or {}, settings, TODAY)


def test_new_item_with_enough_stock_is_published_with_point():
    d = one(run(Evaluation(ok={"100": cand()})))
    assert (d.action, d.price, d.point) == (Action.PUBLISH, 4810, "P1")


def test_new_item_below_threshold_is_skipped():
    assert one(run(Evaluation(ok={"100": cand(stock=3)}))).action is Action.SKIP


def test_published_item_absent_from_supplier_is_removed():
    d = one(run(Evaluation(), published={"100": pub()}))
    assert (d.action, d.reason) == (Action.REMOVE, "absent_from_supplier")


def test_published_item_excluded_by_filter_is_removed_with_reason():
    d = one(run(Evaluation(excluded={"100": "not_enabled"}), published={"100": pub()}))
    assert (d.action, d.reason) == (Action.REMOVE, "not_enabled")


def test_low_stock_removes_published_and_marks_suppressed():
    d = one(run(Evaluation(ok={"100": cand(stock=3)}), published={"100": pub()}))
    assert (d.action, d.reason, d.suppressed) == (Action.REMOVE, "low_stock", True)


def test_suppressed_item_returns_as_soon_as_stock_reaches_the_threshold():
    """Owner's rule: removed below the threshold, back at the threshold: the settings never carry a hysteresis."""
    assert make_settings(hysteresis=7).hysteresis == 0
    assert one(run(Evaluation(ok={"100": cand(stock=3)}), suppressed={"100"})).action is Action.SKIP
    assert one(run(Evaluation(ok={"100": cand(stock=4)}), suppressed={"100"})).action is Action.PUBLISH


def test_the_engine_can_still_hold_back_a_position_when_a_hysteresis_is_given_directly():
    import dataclasses
    s = dataclasses.replace(S, hysteresis=1)
    assert one(run(Evaluation(ok={"100": cand(stock=4)}), suppressed={"100"}, settings=s)).action is Action.SKIP
    assert one(run(Evaluation(ok={"100": cand(stock=5)}), suppressed={"100"}, settings=s)).action is Action.PUBLISH


def test_known_point_is_reused_for_returning_item():
    d = one(run(Evaluation(ok={"100": cand()}), suppressed={"100"}, known={"100": "P3"}))
    assert (d.action, d.point) == (Action.PUBLISH, "P3")


def test_problem_on_published_item_holds_then_goes_stale():
    ev = Evaluation(problems={"100": ["bad_price:0.0"]})
    held = one(run(ev, published={"100": pub()}))
    assert (held.action, held.first_hold, held.price) == (Action.HOLD, TODAY, 4810)
    old = pub(first_hold=TODAY - timedelta(days=3))
    assert one(run(ev, published={"100": old})).action is Action.REMOVE
    assert one(run(ev, published={"100": old})).reason.startswith("stale:")


def test_with_zero_days_a_published_item_with_a_data_problem_is_taken_off_at_once():
    zero = make_settings(stale_days=0)
    d = one(run(Evaluation(problems={"100": ["bad_price:0.0"]}), published={"100": pub()}, settings=zero))
    assert d.action is Action.REMOVE and d.reason.startswith("stale:")


def test_with_zero_days_an_unavailable_supplier_still_holds_its_ads_for_a_day():
    zero = make_settings(stale_days=0)
    ev = Evaluation(problems={"100": ["supplier_unavailable"]})
    assert one(run(ev, published={"100": pub()}, settings=zero)).action is Action.HOLD                    # a missing file is not a bad row
    old = pub(first_hold=TODAY - timedelta(days=1))
    assert one(run(ev, published={"100": old}, settings=zero)).action is Action.REMOVE


def test_problem_on_unpublished_item_is_skipped():
    d = one(run(Evaluation(problems={"100": ["model_needs_mapping:X"]})))
    assert d.action is Action.SKIP


def test_a_supplier_price_change_of_any_size_in_either_direction_is_applied_never_held():
    up = one(run(Evaluation(ok={"100": cand(base=10000.0, price=9500)}), published={"100": pub(base=5000.0)}))
    down = one(run(Evaluation(ok={"100": cand(base=1000.0, price=950)}), published={"100": pub(base=5000.0)}))
    assert (up.action, up.price) == (Action.UPDATE, 9500) and (down.action, down.price) == (Action.UPDATE, 950)


def test_price_change_updates_and_same_price_keeps():
    changed = one(run(Evaluation(ok={"100": cand(price=4900)}), published={"100": pub()}))
    same = one(run(Evaluation(ok={"100": cand()}), published={"100": pub()}))
    assert (changed.action, changed.price) == (Action.UPDATE, 4900)
    assert same.action is Action.KEEP


def test_own_point_item_is_kept_even_when_absent_at_supplier():
    d = one(run(Evaluation(), published={"100": pub(location="own", point="P2")}))
    assert (d.action, d.reason, d.point) == (Action.KEEP, "own_point", "P2")


def test_mass_removal_guard_converts_removals_to_holds():
    published = {str(i): pub(gid=str(i)) for i in range(20)}
    ds = run(Evaluation(), published=published)
    assert ds.guard_tripped is True
    assert {d.action for d in ds.decisions} == {Action.HOLD}
    assert ds.decisions[0].reason.startswith("guard:")


def test_guard_can_be_disabled_explicitly():
    published = {str(i): pub(gid=str(i)) for i in range(20)}
    ds = run(Evaluation(), published=published, settings=make_settings(allow_mass_removal=True))
    assert ds.guard_tripped is False
    assert {d.action for d in ds.decisions} == {Action.REMOVE}


def test_few_removals_do_not_trip_guard():
    published = {str(i): pub(gid=str(i)) for i in range(5)}
    ds = run(Evaluation(), published=published)
    assert ds.guard_tripped is False


def test_the_guard_counts_what_the_light_passes_took_off_within_the_hour():
    published = {str(i): pub(gid=str(i)) for i in range(5)}                 # 5 leave; the threshold is 10
    assert decide(Evaluation(), published, set(), {}, make_settings(), date(2026, 10, 24), removed_before=5).guard_tripped is False
    ds = decide(Evaluation(), published, set(), {}, make_settings(), date(2026, 10, 24), removed_before=6)
    assert ds.guard_tripped is True and {d.action for d in ds.decisions} == {Action.HOLD}
    kept = {str(i): pub(gid=str(i)) for i in range(5)}                      # nothing leaves now: earlier removals alone trip nothing
    ev = Evaluation(ok={str(i): cand(gid=str(i)) for i in range(5)})
    assert decide(ev, kept, set(), {}, make_settings(), date(2026, 10, 24), removed_before=50).guard_tripped is False


def test_portion_limit_defers_the_rest():
    ev = Evaluation(ok={str(i): cand(gid=str(i)) for i in range(5)})
    ds = run(ev, settings=make_settings(portion_limit=2))
    actions = [d.action for d in sorted(ds.decisions, key=lambda d: d.goods_id)]
    assert actions == [Action.PUBLISH, Action.PUBLISH, Action.DEFER, Action.DEFER, Action.DEFER]


def test_zero_stock_removes_even_when_item_also_has_a_problem():
    ev = Evaluation(problems={"100": ["model_needs_mapping:X"]}, stocks={"100": 0})
    d = one(run(ev, published={"100": pub()}))
    assert (d.action, d.reason, d.suppressed) == (Action.REMOVE, "low_stock", True)


def test_problem_with_enough_stock_still_holds():
    ev = Evaluation(problems={"100": ["model_needs_mapping:X"]}, stocks={"100": 10})
    assert one(run(ev, published={"100": pub()})).action is Action.HOLD


def test_problem_with_unknown_stock_holds():
    ev = Evaluation(problems={"100": ["bad_stock:"]})
    assert one(run(ev, published={"100": pub()})).action is Action.HOLD


def test_removals_caused_by_switched_off_brand_do_not_trip_the_guard():
    published = {str(i): pub(gid=str(i)) for i in range(20)}
    ev = Evaluation(excluded={str(i): "not_enabled" for i in range(20)})
    ds = run(ev, published=published)
    assert ds.guard_tripped is False
    assert {d.action for d in ds.decisions} == {Action.REMOVE}
