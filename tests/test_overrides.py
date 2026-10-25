from __future__ import annotations

from datetime import date

import pytest

from helpers import make_offer, make_settings, settings_dict
from tires_sync.candidates import Candidate, Evaluation, evaluate
from tires_sync.catalog import parse_reference
from tires_sync.config import settings_from_dict
from tires_sync.decide import decide
from tires_sync.models import Action, PublishedItem
from tires_sync.overrides import override_key

REF = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
TODAY = date(2026, 10, 6)


def settings(**overrides_map):
    return make_settings(overrides={override_key(g): v for g, v in overrides_map.items()}) if overrides_map else make_settings()


def run(offers, **ov):
    return evaluate({o.goods_id: o for o in offers}, {}, settings(**ov), REF, {})


def test_key_has_the_supplier_namespace_and_values_are_validated():
    assert override_key("100") == "1:100"
    with pytest.raises(ValueError):
        settings_from_dict(settings_dict(overrides={"1:100": "maybe"}))
    assert make_settings().overrides == {}


def test_off_hides_a_position_that_would_otherwise_be_published():
    ev = run([make_offer()], **{"100": "off"})
    assert ev.ok == {} and ev.excluded["100"] == "manual_off"


def test_on_bypasses_the_filters_that_only_hide_goods():
    for kw in ({"diameter": 13}, {"car_type": "C"}, {"name": "IKON 205/55/16 старше 3-х лет"},
               {"brand_key": "IKON TYRES", "season": make_offer().season}):
        ev = run([make_offer(**kw)], **{"100": "on"})
        assert "100" in ev.ok, kw
        assert "100" in ev.forced
    plain = run([make_offer(diameter=13)])
    assert "100" not in plain.ok and plain.excluded["100"] == "diameter"


def test_on_for_a_combination_that_is_not_enabled():
    other = make_offer(season=make_offer().season, brand_key="LAUFENN")
    ev = evaluate({"100": other}, {}, make_settings(overrides={"1:100": "on"}), REF, {})
    assert "100" not in ev.ok  # no price rule and brand not in the reference: still a problem
    assert "100" in ev.problems


def test_on_never_bypasses_what_the_ad_cannot_be_built_without():
    assert "100" in run([make_offer(base_price=0.0)], **{"100": "on"}).problems
    assert "100" in run([make_offer(model_raw="Unmapped")], **{"100": "on"}).problems


def cand(stock):
    return Candidate("100", make_offer(stock=stock), 4810, "Ikon Tyres", "Test Model")


def decision(stock, override, published=None):
    s = settings(**({"100": override} if override else {}))
    ds = decide(Evaluation(ok={"100": cand(stock)}, stocks={"100": stock}), published or {}, set(), {}, s, TODAY)
    return [d for d in ds.decisions if d.goods_id == "100"][0]


def test_forced_on_does_not_lower_the_stock_threshold():
    # owner's rule (2026-10-14): fewer than the threshold is never published, a manual «on» lifts exclusions, not the stock rule
    assert decision(2, None).action is Action.SKIP
    assert decision(2, "on").action is Action.SKIP
    assert decision(4, "on").action is Action.PUBLISH
    assert decision(0, "on").action is Action.SKIP


def test_forced_on_does_not_keep_a_published_item_below_the_threshold():
    pub = {"100": PublishedItem("100", 4810, 5000.0, "P1")}
    assert decision(2, None, pub).action is Action.REMOVE
    assert decision(2, "on", pub).action is Action.REMOVE
    assert decision(4, "on", pub).action is Action.KEEP
    assert decision(0, "on", pub).action is Action.REMOVE


def test_off_for_a_published_item_removes_it_with_a_reason():
    pub = {"100": PublishedItem("100", 4810, 5000.0, "P1")}
    ev = evaluate({"100": make_offer()}, {}, settings(**{"100": "off"}), REF, {})
    ds = decide(ev, pub, set(), {}, settings(**{"100": "off"}), TODAY)
    d = [x for x in ds.decisions if x.goods_id == "100"][0]
    assert (d.action, d.reason) == (Action.REMOVE, "manual_off")


def test_overrides_are_an_allowed_setting_with_history(tmp_path):
    from tires_sync import store
    from tires_sync.settings_store import confirm_draft, create_draft, get_active, init_settings
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), "2026-10-07T10:00:00")
    d = create_draft(conn, {"overrides": {"1:100": "off"}}, "2026-10-07T11:00:00")
    confirm_draft(conn, d, "2026-10-07T11:01:00", "owner")
    assert get_active(conn)[1]["overrides"] == {"1:100": "off"}
