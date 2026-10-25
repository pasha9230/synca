"""Owner, 2026-10-15: the Ids of account 4 have nothing of the supplier's code in them: another set of letters and digits."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

from helpers import catalog_xml, commodity_xml
from test_feed_account_rules import NOW, REF, feed, two_accounts
from tires_sync import connections, cycle, mirror_feed

UNLOAD = catalog_xml(commodity_xml(NNOMMODIF="100", SMNFCODE="TS1"), commodity_xml(NNOMMODIF="101", SMNFCODE="TS2"))


def ids(tmp_path, conn, account):
    return {a.findtext("Id") for a in feed(tmp_path, conn, account)}


def test_alias_id_is_stable_letters_and_digits_without_the_code_and_differs_between_accounts():
    one = mirror_feed.alias_id(4, "102398094791")
    assert one == mirror_feed.alias_id(4, "102398094791")                      # the same every cycle
    assert re.fullmatch(r"k[0-9a-z]{11}", one) and "102398094791" not in one and "_" not in one
    assert one != mirror_feed.alias_id(2, "102398094791") and one != mirror_feed.alias_id(4, "102398094792")


def test_an_account_with_the_scheme_publishes_alias_ids_and_the_others_keep_theirs(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    mirror_feed.set_alias_scheme(conn, second, True, NOW, "t")
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    plain = ids(tmp_path, conn, first)
    assert plain and not any(re.fullmatch(r"k[0-9a-z]{11}", i) for i in plain)             # the primary account is not touched
    got = ids(tmp_path, conn, second)
    assert got and all(re.fullmatch(r"k[0-9a-z]{11}", i) for i in got)
    assert not any(code in i for i in got for code in ("100", "101", "TS1", "TS2"))
    assert not (got & ids(tmp_path, conn, first))
    token = connections.get_account(conn, second)["feed_token"]
    ads = ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot().findall("Ad")
    assert all("/img/%s/" % token in a.find("Images/Image").get("url") for a in ads)      # the photo link is the account's own, as before


def test_the_alias_is_remembered_and_a_new_position_gets_one_too(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    mirror_feed.set_alias_scheme(conn, second, True, NOW, "t")
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    before = ids(tmp_path, conn, second)
    bound = mirror_feed.adopted_maps(conn, second)[0]
    assert set(bound.values()) == before                                           # remembered with the other bindings
    more = catalog_xml(commodity_xml(NNOMMODIF="100", SMNFCODE="TS1"), commodity_xml(NNOMMODIF="101", SMNFCODE="TS2"),
                       commodity_xml(NNOMMODIF="102", SMNFCODE="TS3"))
    cycle.run_for_accounts(conn, more, REF, date(2026, 10, 15), tmp_path / "feed")
    after = ids(tmp_path, conn, second)
    assert before <= after and len(after) == len(before) + 1 and all(re.fullmatch(r"k[0-9a-z]{11}", i) for i in after)


def test_the_scheme_can_be_switched_off_and_is_off_by_default(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    assert mirror_feed.alias_scheme(conn, second) is False
    mirror_feed.set_alias_scheme(conn, second, True, NOW, "t")
    assert mirror_feed.alias_scheme(conn, second) is True
    mirror_feed.set_alias_scheme(conn, second, False, NOW, "t")
    assert mirror_feed.alias_scheme(conn, second) is False


def test_an_older_binding_of_the_account_is_replaced_by_the_alias(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    mirror_feed.bind_ids(conn, second, {"100": "100_a%d" % second}, NOW, "same_tire")           # what an earlier cycle remembered
    mirror_feed.set_alias_scheme(conn, second, True, NOW, "t")
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    got = ids(tmp_path, conn, second)
    assert got and all(re.fullmatch(r"k[0-9a-z]{11}", i) for i in got)
    assert mirror_feed.adopted_maps(conn, second)[0]["100"] == mirror_feed.alias_id(second, "100")


def test_the_check_of_a_published_feed_knows_the_codes_behind_alias_ids(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    mirror_feed.set_alias_scheme(conn, second, True, NOW, "t")
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    account = connections.get_account(conn, second)
    folder = tmp_path / "feed" / account["feed_token"]
    (folder / "avito_feed.xml").write_bytes((folder / "next" / "avito_feed.xml").read_bytes())      # as if it were published
    from tires_sync.webmim import parse_unload
    from tires_sync.settings_store import active_settings
    settings = active_settings(conn)
    findings = cycle.gate(conn, account, "generated", connections.effective_settings(conn, second, settings), parse_unload(UNLOAD, settings.territory_rn),
                          REF, tmp_path / "feed", source="published")
    assert [f for f in findings if f.rule == "stock"] == []                                    # not «нет у поставщика» for every alias Id


def test_a_renewed_position_of_a_generated_account_gets_the_new_id_and_keeps_it(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    before = ids(tmp_path, conn, second)
    mirror_feed.reissue_own(conn, second, ["100"], "20261015", NOW, "t")                    # what `mirror-reissue --confirm` stores
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 15), tmp_path / "feed")
    after = ids(tmp_path, conn, second)
    renewed = [i for i in after if i.endswith("_r20261015")]
    assert len(renewed) == 1 and renewed[0].startswith("100_a%d_" % second)                  # the Id of the account with the stamp
    assert len(after) == len(before) and len(after - set(renewed)) == len(before) - 1       # the other positions keep theirs
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 16), tmp_path / "feed")         # and the next cycle does not change it again
    assert ids(tmp_path, conn, second) == after
    assert ids(tmp_path, conn, first) and not any(i.endswith("_r20261015") for i in ids(tmp_path, conn, first))


def test_the_photo_link_of_a_renewed_or_aliased_ad_carries_its_id_and_the_others_keep_theirs(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    token = connections.get_account(conn, second)["feed_token"]
    urls = lambda: {a.findtext("Id"): a.find("Images/Image").get("url") for a in feed(tmp_path, conn, second)}
    assert all("?" not in u for u in urls().values())                                       # nothing renewed: the links are as before
    mirror_feed.reissue_own(conn, second, ["100"], "20261015", NOW, "t")
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 15), tmp_path / "feed")
    got = urls()
    renewed = [i for i in got if i.endswith("_r20261015")]
    assert len(renewed) == 1 and got[renewed[0]].endswith("/img/%s/100.jpg?v=%s" % (token, renewed[0]))
    assert all("?" not in u for i, u in got.items() if i not in renewed)
    mirror_feed.set_alias_scheme(conn, second, True, NOW, "t")
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 16), tmp_path / "feed")
    assert all("?v=%s" % i in u for i, u in urls().items())                                  # an aliased ad: its own address for the same picture


def test_the_check_reads_an_id_by_its_live_position_when_a_gone_one_shares_it(tmp_path):
    # 2026-10-21: account 1 had Id 113105674045 bound both to its own code and to a gone code 125452774237;
    # the stand-alone feed-check read the gone code and stopped every deploy with «нет у поставщика».
    import json
    from tires_sync import store
    from tires_sync.settings_store import active_settings
    from tires_sync.webmim import parse_unload
    conn, first, second = two_accounts(tmp_path)
    store.set_state(conn, "adopted_%d" % first, json.dumps({"ids": {"100": "100", "999": "100"}, "images": {}, "shared": {}}))
    conn.commit()
    cycle.run_for_accounts(conn, UNLOAD, REF, date(2026, 10, 14), tmp_path / "feed")
    account = connections.get_account(conn, first)
    folder = tmp_path / "feed" / account["feed_token"]
    (folder / "avito_feed.xml").write_bytes((folder / "next" / "avito_feed.xml").read_bytes())
    assert "100" in {a.findtext("Id") for a in feed(tmp_path, conn, first)}
    settings = active_settings(conn)
    findings = cycle.gate(conn, account, "generated", connections.effective_settings(conn, first, settings), parse_unload(UNLOAD, settings.territory_rn),
                          REF, tmp_path / "feed", source="published")
    assert [f for f in findings if f.rule == "stock"] == []


def test_bound_codes_pick_the_code_the_build_keeps():
    from types import SimpleNamespace as O
    offers = {"100": O(stock=12), "200": O(stock=112), "300": O(stock=2)}
    # one tire under two codes, both at the threshold: more stock wins, as in same_tire.dedupe
    assert cycle._bound_codes({"100": "100", "200": "100"}, offers, 4) == {"100": "200"}
    # ... unless the other one is the published one
    assert cycle._bound_codes({"100": "100", "200": "100"}, offers, 4, published={"100"}) == {}
    # the inherited code is below the threshold: the build keeps the own one
    assert cycle._bound_codes({"100": "100", "300": "100"}, offers, 4) == {}
    # the inherited code is gone: the Id is its own position again
    assert cycle._bound_codes({"100": "100", "999": "100"}, offers, 4) == {}
    # an Id with only an inherited binding is read as before, live or not
    assert cycle._bound_codes({"999": "100"}, offers, 4) == {"100": "999"}
    assert cycle._bound_codes({"300": "100"}, offers, 4) == {"100": "300"}
