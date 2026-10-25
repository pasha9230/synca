from __future__ import annotations

import pytest

from helpers import make_offer, make_settings
from panel_helpers import login, make_app, post
from tires_sync import store
from tires_sync.models import Season
from tires_sync.panel import service
from tires_sync.settings_store import active_settings, get_active


def test_rule_matches_brand_season_and_radius_with_wildcards():
    s = make_settings(pack4_rules=["IKON TYRES|winter|16", "*|summer|*"])
    assert s.pack4 and s.pack4_for(make_offer(diameter=16, season=Season.WINTER))
    assert not s.pack4_for(make_offer(diameter=17, season=Season.WINTER))
    assert not s.pack4_for(make_offer(brand_key="PIRELLI", diameter=16, season=Season.WINTER))
    assert s.pack4_for(make_offer(brand_key="PIRELLI", diameter=19, season=Season.SUMMER))


def test_no_rules_means_no_pack_ads_and_the_old_switch_still_means_everything():
    assert not make_settings(pack4_rules=[]).pack4
    assert not make_settings(pack4_rules=[]).pack4_for(make_offer())
    legacy = make_settings(pack4=True)
    assert legacy.pack4 and legacy.pack4_for(make_offer(diameter=22))


@pytest.mark.parametrize("bad", ["x", "IKON|winter", "IKON|spring|16", "IKON|winter|abc", [1]])
def test_bad_rules_are_rejected(bad):
    with pytest.raises(ValueError):
        make_settings(pack4_rules=bad)


def test_cycle_publishes_the_twin_only_where_a_rule_matches(tmp_path):
    from helpers import commodity_xml
    from test_sync import REF, TODAY, run_cycle, xml

    def ids(rules):
        conn = store.connect(tmp_path / ("t%s.db" % rules[0][-2:]))
        out = tmp_path / ("o%s" % rules[0][-2:])
        run_cycle(xml(commodity_xml(NNOMMODIF="100", NREST=10)), make_settings(pack4_rules=rules), REF, conn, TODAY, out_dir=out)
        feed = (out / "avito_feed.xml").read_text(encoding="utf-8")
        return [i for i in ("<Id>100</Id>", "<Id>100-x4</Id>") if i in feed]

    assert ids(["*|*|16"]) == ["<Id>100</Id>", "<Id>100-x4</Id>"]
    assert ids(["*|*|17"]) == ["<Id>100</Id>"]


def test_settings_form_adds_and_removes_rules_through_a_draft(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    page = client.get("/settings").get_data(as_text=True)
    assert 'name="p4_brand"' in page and 'name="p4_season"' in page and 'name="p4_d"' in page
    r = post(client, "/settings/draft", tok, pack4_form="1", p4_add="1", p4_brand="IKON TYRES", p4_season="winter", p4_d="16")
    assert "IKON TYRES · зима · R16" in client.get(r.headers["Location"]).get_data(as_text=True)
    post(client, r.headers["Location"] + "/confirm", tok)
    conn = store.connect(app.config["DB_PATH"])
    assert get_active(conn)[1]["pack4_rules"] == ["IKON TYRES|winter|16"] and active_settings(conn).pack4
    assert "IKON TYRES · зима · R16" in client.get("/settings").get_data(as_text=True)
    r = post(client, "/settings/draft", tok, pack4_form="1", p4_del="IKON TYRES|winter|16")
    post(client, r.headers["Location"] + "/confirm", tok)
    assert not active_settings(store.connect(app.config["DB_PATH"])).pack4


def test_forms_without_the_pack4_marker_do_not_touch_the_rules(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    r = post(client, "/settings/draft", tok, pack4_form="1", p4_add="1", p4_brand="*", p4_season="*", p4_d="*")
    post(client, r.headers["Location"] + "/confirm", tok)
    r = post(client, "/settings/draft", tok, min_stock="5")
    post(client, r.headers["Location"] + "/confirm", tok)
    assert get_active(store.connect(app.config["DB_PATH"]))[1]["pack4_rules"] == ["*|*|*"]


def test_rule_text_reads_in_words():
    assert service.pack4_rule_text("*|*|*") == "все бренды · все сезоны · все радиусы"
    assert service.pack4_rule_text("S2:PIRELLI|all|18") == "PIRELLI (поставщик 2) · всесезонные · R18"


def test_removed_rule_clears_the_twin_so_a_later_hold_does_not_bring_it_back(tmp_path):
    from datetime import timedelta
    from helpers import catalog_xml, commodity_xml
    from test_review4_fixes import NOW, REF, TODAY, env, feed_of, goods
    from tires_sync import cycle, publishing
    from tires_sync.settings_store import confirm_draft, create_draft
    conn, _, _ = env(tmp_path, pack4_rules=["*|*|*"], guard_min=5)
    cycle.run_for_accounts(conn, goods(4), REF, TODAY, tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    assert "<Id>1-x4</Id>" in feed_of(tmp_path, conn)
    confirm_draft(conn, create_draft(conn, {"pack4_rules": ["*|*|30"], "pack4": True}, NOW), NOW, "owner")
    cycle.run_for_accounts(conn, goods(4), REF, TODAY + timedelta(days=1), tmp_path / "feed")
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    assert "x4" not in feed_of(tmp_path, conn)
    jumped = catalog_xml(
        commodity_xml(NNOMMODIF="1", NREST="12", SMNFCODE="C1", NPRICE_DELAYED="по запросу"),
        *[commodity_xml(NNOMMODIF=str(i), NREST="12", SMNFCODE="C%d" % i) for i in (2, 3, 4)])
    results = cycle.run_for_accounts(conn, jumped, REF, TODAY + timedelta(days=2), tmp_path / "feed")
    assert results[0].ok and results[0].counts.get("hold") == 1                       # bad data: held, not taken off (the wait is 3 days here)
    assert "x4" not in feed_of(tmp_path, conn)


def test_an_old_draft_with_the_bare_pack4_switch_still_reads_in_the_review():
    from helpers import settings_dict
    from tires_sync.panel.service import describe_changes
    assert any("для всех" in line for line in describe_changes(settings_dict(), {"pack4": True}))
    lines = describe_changes(settings_dict(), {"pack4": True, "pack4_rules": ["IKON TYRES|winter|16"]})
    assert any("IKON TYRES · зима · R16" in line for line in lines) and not any("для всех" in line for line in lines)


def test_adding_a_rule_without_a_brand_is_explained_and_changes_nothing(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    tok = login(client)
    r = post(client, "/settings/draft", tok, pack4_form="1", p4_add="1", p4_brand="", p4_season="winter", p4_d="16")
    assert r.headers["Location"].endswith("/settings")
    assert "Выберите бренд" in client.get("/settings").get_data(as_text=True)
    r = post(client, "/settings/draft", tok, pack4_form="1", p4_brand="", p4_season="winter", p4_d="16", min_stock="5")
    assert get_active(store.connect(app.config["DB_PATH"]))[1].get("pack4_rules") is None and r.status_code == 302


def test_rule_text_marks_the_supplier_and_the_typographic_minus_is_accepted():
    assert service.pack4_rule_text("S2:PIRELLI|all|18") == "PIRELLI (поставщик 2) · всесезонные · R18"
    assert service._float("\u22123,8", "x") == -3.8


def test_rule_pattern_is_strict():
    with pytest.raises(ValueError):
        make_settings(pack4_rules=["IKON|winter|16\n"])
    with pytest.raises(ValueError):
        make_settings(pack4_rules=["IKON|winter|\u0661\u0666"])


def test_ads_gone_counts_a_twin_only_for_positions_the_rules_cover(tmp_path):
    from tires_sync.panel.service import ads_gone
    offers = {"1": make_offer(goods_id="1", diameter=16, stock=10), "2": make_offer(goods_id="2", diameter=17, stock=10),
              "3": make_offer(goods_id="3", diameter=16, stock=2)}
    rules16 = make_settings(pack4_rules=["*|*|16"])
    assert ads_gone(["1", "2", "3"], offers, rules16) == 4          # 3 positions + the twin of «1» only (17 is not a rule, 3 has no stock)
    assert ads_gone(["1", "2"], offers, make_settings()) == 2       # no rules, no twins
    assert ads_gone(["9"], offers, rules16) == 1                    # unknown position: just itself


def test_the_shrink_check_compares_ads_with_ads(tmp_path):
    from tires_sync import connections
    from tires_sync.models import PublishedItem
    conn = store.connect(tmp_path / "t.db")
    store._upsert(conn, PublishedItem("1", 100, 100, "P1"), "snap", 1, "snap4")
    store._upsert(conn, PublishedItem("2", 100, 100, "P1"), "snap", 1, None)
    conn.commit()
    assert store.count_published_ads(conn, 1) == 3 and store.count_published_ads(conn, 2) == 0


def test_ads_gone_also_counts_twins_that_vanish_when_the_rules_are_narrowed():
    from tires_sync.panel.service import ads_gone
    offers = {"1": make_offer(goods_id="1", diameter=16, stock=10), "2": make_offer(goods_id="2", diameter=17, stock=10)}
    everything, only17 = make_settings(pack4_rules=["*|*|*"]), make_settings(pack4_rules=["*|*|17"])
    assert ads_gone([], offers, everything, only17) == 1            # the twin of «1» goes, the position stays
    assert ads_gone([], offers, everything, make_settings()) == 2   # the feature switched off: both twins
    assert ads_gone(["1"], offers, everything, only17) == 2         # position and its twin, not counted twice


def test_twins_of_an_old_snapshot_are_not_counted_once_the_feature_is_off(tmp_path):
    from tires_sync.models import PublishedItem
    conn = store.connect(tmp_path / "t.db")
    store._upsert(conn, PublishedItem("1", 100, 100, "P1"), "snap", 1, "snap4")
    conn.commit()
    assert store.count_published_ads(conn, 1) == 2 and store.count_published_ads(conn, 1, twins=False) == 1
