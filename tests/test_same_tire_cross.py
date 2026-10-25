"""same_tire with tire_match (the switch on): one tire at WebMIM and 4tochki is one ad, whatever Avito models the two sides are mapped to."""
from __future__ import annotations

import json

from helpers import make_offer
from tires_sync import same_tire, tire_match
from tires_sync.candidates import Candidate, Evaluation
from tires_sync.models import Season

W, T = "100", "s2-TCH1"


def offers(w_stock=12, t_stock=41):
    common = dict(width=155, height=80, diameter=13, load="79", speed="T", season=Season.WINTER, studded=True)
    return {W: make_offer(goods_id=W, brand_raw="Ikon Tyres", brand_key="IKON TYRES", model_raw="Ikon Character Ice 7", stock=w_stock,
                          name="Ikon Tyres 155/80/13 T 79 Ikon Character Ice 7 Ш.", name_orig="", **common),
            T: make_offer(goods_id=T, brand_raw="IKON", brand_key="S2:IKON", model_raw="Character Ice 7 (Nordman 7)", stock=t_stock,
                          name="IKON 155/80R13 79T Character Ice 7 (Nordman 7) TL (шип.)", name_orig="", **common)}


def evaluation(o, codes=(W, T)):
    models = {W: "Character Ice 7", T: "Nordman 7"}
    return Evaluation(ok={g: Candidate(g, o[g], 5000, "Ikon Tyres", models[g]) for g in codes})


def cross(o):
    return tire_match.pairs(o, {}, 2)


def ids(code):
    return code


def test_off_the_two_sides_stay_two_ads():
    o = offers()
    ev = evaluation(o)
    assert same_tire.dedupe(ev, (), 4) == {} and set(ev.ok) == {W, T}


def test_on_a_new_tire_takes_webmim_with_less_stock_and_its_avito_model():
    o = offers(w_stock=12, t_stock=41)
    ev = evaluation(o)
    same_tire.dedupe(ev, (), 4, cross=cross(o))
    assert set(ev.ok) == {W} and ev.excluded[T] == "same_tire" and ev.ok[W].avito_model == "Character Ice 7"


def test_on_a_live_published_4tochki_ad_stays_and_takes_the_avito_model_of_webmim():
    o = offers()
    ev = evaluation(o)
    same_tire.dedupe(ev, {T}, 4, live_id_of=ids, cross=cross(o))
    assert set(ev.ok) == {T} and ev.ok[T].avito_model == "Character Ice 7" and ev.excluded[W] == "same_tire"


def test_on_webmim_below_the_threshold_4tochki_takes_its_id():
    o = offers(w_stock=2)
    ev = evaluation(o)
    inherit = same_tire.dedupe(ev, {W}, 4, live_id_of=ids, first_seen_of=lambda i: "2026-09-01" if i == W else None, cross=cross(o))
    assert inherit == {T: W} and T in ev.ok


def test_on_a_retired_webmim_ad_is_inherited_through_the_key_of_its_side():
    o = offers(w_stock=0)
    ev = evaluation(o)
    key_w = same_tire.identity(ev.ok[W])
    retired = [(W, "100", tuple(json.loads(json.dumps(list(key_w)))), "2026-10-20T10:00:00")]
    assert same_tire.dedupe(evaluation(o), (), 4, live_id_of=ids, retired=retired) == {}           # off: another Avito model, a new Id
    inherit = same_tire.dedupe(ev, (), 4, live_id_of=ids, retired=retired, cross=cross(o))
    assert inherit == {T: "100"}


def test_on_an_id_another_account_has_is_never_handed_over():
    o = offers(w_stock=0)
    ev = evaluation(o)
    retired = [(W, "100", same_tire.identity(ev.ok[W]), "2026-10-20T10:00:00")]
    assert same_tire.dedupe(ev, (), 4, live_id_of=ids, retired=retired, foreign_ids={"100"}, cross=cross(o)) == {}


def test_on_a_published_webmim_ad_held_out_of_the_candidates_is_recognised_by_its_saved_ad():
    o = offers()
    ev = evaluation(o, codes=(T,))                                    # WebMIM is in the unload but not a candidate (held): its saved ad tells the tire
    snapshot = ("<Ad><Id>100</Id><Brand>Ikon Tyres</Brand><Model>Character Ice 7</Model><TireSectionWidth>155</TireSectionWidth>"
                "<TireAspectRatio>80</TireAspectRatio><RimDiameter>13</RimDiameter><TireType>Зимние шипованные</TireType><LoadIndex>79</LoadIndex>"
                "<SpeedIndex>T</SpeedIndex></Ad>")
    inherit = same_tire.dedupe(ev, {W}, 4, live_id_of=ids, snapshot_of=lambda g: snapshot if g == W else None, cross=cross(o))
    assert inherit == {T: "100"} and ev.excluded[W] == "same_tire"   # 4tochki goes on under the Id of the live WebMIM ad
    assert ev.ok[T].avito_model == "Character Ice 7"                 # and the ad keeps its Model
    off = evaluation(o, codes=(T,))
    assert same_tire.dedupe(off, {W}, 4, live_id_of=ids, snapshot_of=lambda g: snapshot if g == W else None) == {}


SNAPSHOT_W = ("<Ad><Id>100</Id><Brand>Ikon Tyres</Brand><Model>Character Ice 7</Model><TireSectionWidth>155</TireSectionWidth>"
              "<TireAspectRatio>80</TireAspectRatio><RimDiameter>13</RimDiameter><TireType>Зимние шипованные</TireType><LoadIndex>79</LoadIndex>"
              "<SpeedIndex>T</SpeedIndex></Ad>")


def remembered(o, here):
    """The pairs found while both sides were in the unload (`o`), as the store keeps them, applied to the positions of now (`here`)."""
    stored = [(a, b, ia, ib, "2026-10-20T10:00:00") for a, b, ia, ib in tire_match.found_pairs(cross(o))]
    result = tire_match.pairs(here, {}, 2)
    tire_match.remember(result, stored, here)
    return result


def test_on_a_remembered_pair_the_gone_webmim_ad_passes_its_id_to_4tochki():
    """The known gap of match-core is closed: the WebMIM row left the unload, its ad is live (saved ad), the 4tochki side is mapped to
    another Avito model. Without the memory 4tochki would come out under a new Id (a repeat for Avito); with it the ad goes on."""
    o = offers()
    only_tochki = {T: o[T]}
    snap = lambda g: SNAPSHOT_W if g == W else None
    no_memory = evaluation(o, codes=(T,))
    assert same_tire.dedupe(no_memory, {W}, 4, live_id_of=ids, snapshot_of=snap, cross=cross(only_tochki)) == {}
    assert no_memory.ok[T].avito_model == "Nordman 7"
    result = remembered(o, only_tochki)
    assert result.groups == [(W, T)] and result.remembered == [(W, T)]
    ev = evaluation(o, codes=(T,))
    inherit = same_tire.dedupe(ev, {W}, 4, live_id_of=ids, snapshot_of=snap, cross=result)
    assert inherit == {T: "100"} and ev.ok[T].avito_model == "Character Ice 7" and ev.excluded[W] == "same_tire"
    assert result.switches == {T: ("100", W)} and result.waiting == {}


def test_on_a_remembered_pair_the_gone_and_retired_webmim_ad_passes_its_id():
    """The WebMIM row left and its ad is off the feed (retired): 4tochki, back above the threshold later, takes its Id. A retired ad has no
    Avito model name, so the ad comes back with the model of 4tochki (match-report says «модель сменится»)."""
    o = offers()
    retired = [(W, "100", same_tire.identity(evaluation(o).ok[W]), "2026-10-20T10:00:00")]
    only_tochki = {T: o[T]}
    assert same_tire.dedupe(evaluation(o, codes=(T,)), (), 4, live_id_of=ids, retired=retired, cross=cross(only_tochki)) == {}
    result = remembered(o, only_tochki)
    ev = evaluation(o, codes=(T,))
    assert same_tire.dedupe(ev, (), 4, live_id_of=ids, retired=retired, cross=result) == {T: "100"}
    assert ev.ok[T].avito_model == "Nordman 7" and result.switches == {T: ("100", W)}


def test_memory_does_not_pair_when_the_side_here_is_another_tire_now():
    o = offers()
    changed = {T: make_offer(**dict(vars(o[T]), load="80"))}                    # the supplier changed the position under the same code
    assert remembered(o, changed).groups == [] and remembered(o, changed).remembered == []


def test_memory_does_not_pair_two_sides_that_are_both_here_and_apart_now():
    o = offers()
    renamed = {W: o[W], T: make_offer(**dict(vars(o[T]), model_raw="Other Name", name="IKON 155/80R13 79T Other Name (шип.)"))}
    assert remembered(o, renamed).groups == []


def test_memory_leaves_a_gone_side_out_of_a_group_with_another_lot_of_its_supplier():
    o = offers()
    here = {T: o[T], "200": make_offer(**dict(vars(o[W]), goods_id="200"))}       # another WebMIM lot of the tire is paired now
    result = remembered(o, here)
    assert result.groups == [("200", T)] and result.remembered == []


def pause(result, last, now, ad_id="100", change=(T, W)):
    """The last change of supplier of the ad `ad_id` (from, to) was at `last`; the cycle runs at `now`."""
    result.last_switch, result.now = ({ad_id: change + (last,)} if last else {}), now
    return result


def test_a_first_change_of_supplier_goes_at_once():
    o = offers(w_stock=2)
    result = pause(cross(o), None, "2026-10-25T10:00:00")
    ev = evaluation(o)
    assert same_tire.dedupe(ev, {W}, 4, live_id_of=ids, cross=result) == {T: "100"} and result.switches == {T: ("100", W)}


def test_a_second_change_of_supplier_within_a_day_waits_and_after_the_day_goes():
    o = offers(w_stock=2)                                                      # WebMIM below the threshold, 4tochki has stock
    ev = evaluation(o)
    early = pause(cross(o), "2026-10-24T12:00:00", "2026-10-25T11:59:59")
    assert same_tire.dedupe(ev, {W}, 4, live_id_of=ids, cross=early) == {}
    assert T not in ev.ok and ev.excluded[T] == "same_tire"                     # neither the Id of WebMIM nor one of its own
    assert W in ev.ok                                                          # WebMIM is judged by the stock rule (taken off as now)
    assert early.waiting == {T: ("100", "2026-10-25T12:00:00")} and early.switches == {}
    ev = evaluation(o)
    late = pause(cross(o), "2026-10-24T12:00:00", "2026-10-25T12:00:00")
    assert same_tire.dedupe(ev, {W}, 4, live_id_of=ids, cross=late) == {T: "100"} and late.switches == {T: ("100", W)}


def test_the_same_change_again_is_not_a_new_one():
    """A cycle noted the change WebMIM -> 4tochki but its feed was not published (the check stopped it, a draft): the next cycle makes the
    same change, it neither waits nor is noted again."""
    o = offers(w_stock=2)
    ev = evaluation(o)
    again = pause(cross(o), "2026-10-25T09:00:00", "2026-10-25T10:00:00", change=(W, T))
    assert same_tire.dedupe(ev, {W}, 4, live_id_of=ids, cross=again) == {T: "100"} and again.waiting == {} and again.switches == {}


def test_within_a_day_a_retired_ad_waits_too():
    o = offers()
    retired = [(W, "100", same_tire.identity(evaluation(o).ok[W]), "2026-10-25T09:00:00")]
    result = pause(remembered(o, {T: o[T]}), "2026-10-25T08:00:00", "2026-10-25T10:00:00")
    ev = evaluation(o, codes=(T,))
    assert same_tire.dedupe(ev, (), 4, live_id_of=ids, retired=retired, cross=result) == {}
    assert T not in ev.ok and result.waiting == {T: ("100", "2026-10-26T08:00:00")}


def test_the_live_source_stays_when_the_other_comes_back_within_a_day():
    o = offers()                                                               # 4tochki took the ad an hour ago, WebMIM is back
    ev = evaluation(o)
    result = pause(cross(o), "2026-10-25T09:00:00", "2026-10-25T10:00:00")
    assert same_tire.dedupe(ev, {T}, 4, live_id_of=ids, cross=result) == {}
    assert set(ev.ok) == {T} and result.waiting == {} and result.switches == {}


def test_another_lot_of_the_same_supplier_is_not_held_by_the_day():
    o = offers(w_stock=2)
    o["200"] = make_offer(**dict(vars(o[W]), goods_id="200", stock=12))
    ev = Evaluation(ok={g: Candidate(g, o[g], 5000, "Ikon Tyres", "Character Ice 7") for g in (W, "200")})
    result = pause(tire_match.pairs({W: o[W], "200": o["200"]}, {}, 2), "2026-10-25T09:00:00", "2026-10-25T10:00:00")
    assert same_tire.dedupe(ev, {W}, 4, live_id_of=ids, cross=result) == {"200": "100"} and result.waiting == {}


def test_a_published_side_is_never_taken_off_by_the_day():
    o = offers()                                                               # both sides live (before the switch): WebMIM keeps the ad
    ev = evaluation(o)
    result = pause(cross(o), "2026-10-25T09:00:00", "2026-10-25T10:00:00", ad_id=T, change=(W, T))
    inherit = same_tire.dedupe(ev, {W, T}, 4, live_id_of=ids, first_seen_of=lambda i: "2026-09-01" if i == T else None, cross=result)
    assert inherit == {W: T} and W in ev.ok and result.waiting == {} and result.switches == {W: (T, T)}


BOUND = {W: "100", T: "100"}.get                                         # 4tochki took the ad 100 of WebMIM (bind_ids): both codes have that Id


def test_webmim_back_under_its_own_id_within_a_day_of_the_change_waits():
    """Review round 1: the ad 100 went over to 4tochki at 09:00, 4tochki is down at 10:00 and WebMIM is back. Both codes have the Id 100 and
    the same date, so no Id is inherited; the change back is still a change of supplier: WebMIM waits, the ad is taken off by the stock rule."""
    o = offers(t_stock=2)
    ev = evaluation(o)
    early = pause(cross(o), "2026-10-25T09:00:00", "2026-10-25T10:00:00", change=(W, T))
    assert same_tire.dedupe(ev, {T}, 4, live_id_of=BOUND, first_seen_of=lambda i: "2026-09-01", cross=early) == {}
    assert W not in ev.ok and ev.excluded[W] == "same_tire" and early.waiting == {W: ("100", "2026-10-26T09:00:00")}
    assert T in ev.ok and early.switches == {}                                # 4tochki below the threshold: the stock rule takes it off
    ev = evaluation(o)
    late = pause(cross(o), "2026-10-25T09:00:00", "2026-10-26T09:00:00", change=(W, T))
    assert same_tire.dedupe(ev, {T}, 4, live_id_of=BOUND, first_seen_of=lambda i: "2026-09-01", cross=late) == {}
    assert W in ev.ok and T in ev.ok and late.waiting == {} and late.switches == {W: ("100", T)}   # after the day: back, and noted


def test_a_retired_ad_of_the_other_supplier_under_the_own_id_is_a_change_too():
    o = offers(t_stock=0)                                                     # 4tochki had the ad 100 and is off the feed now
    retired = [(T, "100", same_tire.identity(evaluation(o).ok[W]), "2026-10-25T09:30:00")]
    ev = evaluation(o)
    result = pause(cross(o), "2026-10-25T09:00:00", "2026-10-25T10:00:00", change=(W, T))
    assert same_tire.dedupe(ev, (), 4, live_id_of=BOUND, retired=retired, cross=result) == {}
    assert W not in ev.ok and result.waiting == {W: ("100", "2026-10-26T09:00:00")}


def test_a_held_live_ad_stays_held_while_the_other_side_waits():
    """Review round 1: the live WebMIM ad is held (a problem, not a candidate); 4tochki waits for the day. WebMIM is not hidden by a code
    that does not go: its problem stays, the cycle holds it as before (not «same_tire»)."""
    o = offers()
    ev = evaluation(o, codes=(T,))
    ev.problems[W] = ["supplier_unavailable"]
    snap = lambda g: SNAPSHOT_W if g == W else None
    result = pause(cross(o), "2026-10-25T09:00:00", "2026-10-25T10:00:00", change=(T, W))
    assert same_tire.dedupe(ev, {W}, 4, live_id_of=ids, snapshot_of=snap, cross=result) == {}
    assert T not in ev.ok and W not in ev.excluded and ev.problems[W] == ["supplier_unavailable"]


def test_memory_is_not_used_for_a_supplier_unavailable_now():
    """Review round 1: the 4tochki file is old (supplier unavailable), its codes are not in the unload: it has not left, its ads are held."""
    o = offers()
    stored = [(a, b, ia, ib, "2026-10-20T10:00:00") for a, b, ia, ib in tire_match.found_pairs(cross(o))]
    result = tire_match.pairs({W: o[W]}, {}, 2)
    tire_match.remember(result, stored, {W: o[W]}, unavailable={2})
    assert result.groups == [] and result.remembered == []
    tire_match.remember(result, stored, {W: o[W]})
    assert result.groups == [(W, T)]


def test_memory_skips_a_doubtful_or_ambiguous_side_here():
    o = offers()
    doubtful = {T: make_offer(**dict(vars(o[T]), name="IKON 175/70R13 79T Character Ice 7 (шип.)"))}   # another size in the name
    assert remembered(o, doubtful).groups == []
    two_models = {T: o[T], "s2-TCH2": make_offer(**dict(vars(o[T]), goods_id="s2-TCH2", model_raw="Nordman 7"))}   # 4tochki: two models
    two_models["200"] = make_offer(**dict(vars(o[W]), goods_id="200", model_raw="Character Ice 7 (Nordman 7)"))
    result = remembered(o, two_models)
    assert result.ambiguous and result.remembered == []


def test_memory_joins_a_second_side_here_to_the_group_of_the_gone_one():
    o = offers()
    t2 = make_offer(**dict(vars(o[T]), goods_id="s2-TCH2"))                    # another 4tochki lot of the tire, paired with WebMIM too
    stored = [(a, b, ia, ib, "2026-10-20T10:00:00") for a, b, ia, ib in tire_match.found_pairs(cross({W: o[W], T: o[T], "s2-TCH2": t2}))]
    here = {T: o[T], "s2-TCH2": t2}
    result = tire_match.pairs(here, {}, 2)
    tire_match.remember(result, stored, here)
    assert result.groups == [(W, T, "s2-TCH2")] and result.remembered == [(W, T, "s2-TCH2")]


def test_a_saved_ad_of_another_size_is_not_taken_for_the_remembered_side():
    o = offers()
    other_size = SNAPSHOT_W.replace("<RimDiameter>13</RimDiameter>", "<RimDiameter>14</RimDiameter>")
    ev = evaluation(o, codes=(T,))
    inherit = same_tire.dedupe(ev, {W}, 4, live_id_of=ids, snapshot_of=lambda g: other_size if g == W else None, cross=remembered(o, {T: o[T]}))
    assert inherit == {} and ev.ok[T].avito_model == "Nordman 7"


def test_on_another_lot_under_the_mapped_avito_model_writes_the_main_model_too():
    o = offers()
    o["s2-TCH2"] = make_offer(**dict(vars(o[T]), goods_id="s2-TCH2", model_raw="Nordman 7", name="x", stock=5))   # same Avito key as T
    ev = evaluation(o)
    ev.ok["s2-TCH2"] = Candidate("s2-TCH2", o["s2-TCH2"], 5000, "Ikon Tyres", "Nordman 7")
    same_tire.dedupe(ev, {"s2-TCH2"}, 4, live_id_of=ids, cross=cross({W: o[W], T: o[T]}))
    assert set(ev.ok) == {"s2-TCH2"} and ev.ok["s2-TCH2"].avito_model == "Character Ice 7"
