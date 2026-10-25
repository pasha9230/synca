"""A tire that comes back under a new supplier code takes the Id of its live ad BEFORE the feed is published, so Avito never sees a duplicate (owner, 2026-10-19)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

import pytest

from helpers import catalog_xml, commodity_xml
from http_fakes import ScriptedHttp
from test_heal import FakeClient
from test_multi_supplier_cycle import ONE, files, prepared
from test_same_tire import REF, ids, run, setup
from tires_sync import same_tire, cli, connections, cycle, mirror_feed, store
from tires_sync.cli import main
from tires_sync.webmim import parse_unload
import json

SAME = dict(SLOAD="94", SWIDTH="205")


def unload(code, stock=8):
    return catalog_xml(commodity_xml(NNOMMODIF=code, NREST=str(stock), SMNFCODE="LOT-" + code, **SAME))


def built(conn, tmp_path, xml, day=15):
    results = cycle.run_for_accounts(conn, xml, REF, date(2026, 10, day), tmp_path / "feed")
    assert results[0].ok, results[0].error
    return results


def cabinet_for(conn, tmp_path, account, ad_id, status="active"):
    """A Avito cabinet in which the first ad of the tire lives under `ad_id` (its title is the one Avito makes from the ad of the feed)."""
    token = connections.get_account(conn, account)["feed_token"]
    one = ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot().findall("Ad")[0]
    title = "%s %s 205/55 R16 94%s" % (one.findtext("Brand"), one.findtext("Model"), one.findtext("SpeedIndex"))
    return Cabinet([{"id": 77, "xml": ad_id, "title": title}], [])


def old_copy(conn, tmp_path, account, ad_id, runflat=None):
    """The memory of the old ad `ad_id` (taken off our feed days ago): the identity of the ad of the feed, optionally with the RunFlat kind flipped."""
    token = connections.get_account(conn, account)["feed_token"]
    one = ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot().findall("Ad")[0]
    key = list(same_tire.identity_of_ad(ET.tostring(one, encoding="unicode")))
    if runflat is not None:
        key[8] = runflat
    conn.execute("INSERT OR REPLACE INTO retired_ad (account_id, goods_id, ad_id, identity, removed_at) VALUES (?,?,?,?,?)",
                 (account, "OLD-" + ad_id, ad_id, json.dumps(key, ensure_ascii=False), "2026-10-10T10:00:00"))
    conn.commit()


class Cabinet(FakeClient):
    """The fake cabinet of test_heal that also knows the `retry` flag (the cycle reads Avito without retries) and remembers it."""
    retries: list = []

    def items(self, status="active", per_page=100, retry=True):
        Cabinet.retries.append(retry)
        return super().items(status, per_page)

    def ad_ids_for(self, ids, retry=True):
        Cabinet.retries.append(retry)
        return super().ad_ids_for(ids)


def prebind(conn, tmp_path, results, client, xml=None, **kw):
    """_prebind_accounts with the unload of the cycle parsed (the supplier's kinds of the tire are checked against it)."""
    parsed = parse_unload(xml, "T1") if xml is not None else kw.pop("parsed", None)
    return cli._prebind_accounts(conn, None, tmp_path / "feed", results, parsed=parsed, client_for=lambda acc: client)


def test_the_new_code_takes_the_id_of_the_live_ad_before_the_feed_is_published(tmp_path):
    conn, account = setup(tmp_path)                                                    # the old ad lives in the cabinet only (its code left our feed days ago)
    results = built(conn, tmp_path, unload("125"))                                     # the supplier gives the tire now under a new code
    assert ids(tmp_path, conn, account) == ["125"]                                     # a new Id: Avito would block it as a repeat
    old_copy(conn, tmp_path, account, "106")
    Cabinet.retries = []
    applied = prebind(conn, tmp_path, results, cabinet_for(conn, tmp_path, account, "106"), unload("125"))
    assert [(a["id"], [(e["code"], e["orphan"]) for e in b]) for a, b, *_ in applied] == [(account, [("125", "106")])]
    assert set(Cabinet.retries) == {False}                                             # no waiting on Avito's 429 answers while the cycle holds the lock
    assert mirror_feed.adopted_maps(conn, account)[0]["125"] == "106"
    built(conn, tmp_path, unload("125"))                                               # the feeds are built again
    assert ids(tmp_path, conn, account) == ["106"]                                     # the position goes out under the Id of the live ad
    token = connections.get_account(conn, account)["feed_token"]
    title = ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot().findtext("Ad/Title")
    assert title == cabinet_for(conn, tmp_path, account, "106").active[0]["title"]     # and keeps the title it has on Avito
    assert prebind(conn, tmp_path, results, cabinet_for(conn, tmp_path, account, "106"), unload("125")) == []      # bound already: nothing to do the next cycle


def test_nothing_is_bound_when_no_ad_of_the_tire_is_alive_or_the_tire_is_already_under_the_live_id(tmp_path):
    conn, account = setup(tmp_path)
    results = built(conn, tmp_path, unload("106"))
    live = cabinet_for(conn, tmp_path, account, "106")                                 # the live ad has the Id our feed already has: nothing to bind
    assert prebind(conn, tmp_path, results, live, unload("106")) == []
    new = built(conn, tmp_path, unload("125"))
    assert prebind(conn, tmp_path, new, Cabinet([], []), unload("125")) == []          # an empty cabinet: a new Id is right
    blocked = Cabinet([dict(live.active[0], xml="106")], [])
    blocked.items = lambda status="active", per_page=100, retry=True: iter([] if status == "active" else blocked.active)      # a blocked ad is not a live one
    assert prebind(conn, tmp_path, new, blocked, unload("125")) == []
    assert mirror_feed.adopted_maps(conn, account)[0] == {} and ids(tmp_path, conn, account) == ["125"]


def test_a_cabinet_that_does_not_answer_or_a_feed_that_is_not_live_changes_nothing(tmp_path, capsys):
    conn, account = setup(tmp_path)
    results = built(conn, tmp_path, unload("125"))

    def broken(acc):
        raise OSError("handshake timed out")
    assert cli._prebind_accounts(conn, None, tmp_path / "feed", results, parsed=parse_unload(unload("125"), "T1"), client_for=broken) == []
    assert "привязка Id до публикации не выполнена" in capsys.readouterr().err and mirror_feed.adopted_maps(conn, account)[0] == {}
    connections.set_live(conn, account, False, "2026-10-15T10:00:00", "o")
    assert prebind(conn, tmp_path, results, cabinet_for(conn, tmp_path, account, "106"), unload("125")) == []
    assert mirror_feed.adopted_maps(conn, account)[0] == {}


def test_an_incomplete_picture_of_the_cabinet_binds_nothing(tmp_path, capsys):
    conn, account = setup(tmp_path)
    results = built(conn, tmp_path, unload("125"))
    old_copy(conn, tmp_path, account, "106")
    client = cabinet_for(conn, tmp_path, account, "106")
    client.active += [{"id": 100 + i, "xml": None, "title": "Brand%d Model 205/55 R16 94T" % i} for i in range(cli.MANUAL_LIMIT + 1)]   # too many without their Id
    client.ad_ids_for = lambda ids, retry=True: {i["id"]: i["xml"] for i in client.active if i["id"] in ids and i["xml"]}
    assert prebind(conn, tmp_path, results, client, unload("125")) == []
    assert "пришла без Id из файла" in capsys.readouterr().err and mirror_feed.adopted_maps(conn, account)[0] == {}
    assert mirror_feed.manual_ads(conn, account) == []                                           # an incomplete picture is not remembered either


def test_one_manual_ad_does_not_stop_the_binding_of_the_account_and_is_remembered(tmp_path):
    """Owner, 2026-10-19: «Партнёр-2» has one ad switched on by hand; it no longer turns the binding off for the whole account."""
    conn, account = setup(tmp_path)
    results = built(conn, tmp_path, unload("125"))
    old_copy(conn, tmp_path, account, "106")
    client = cabinet_for(conn, tmp_path, account, "106")
    client.active.append({"id": 78, "xml": None, "title": "Kumho WinterCraft Ice Wi32 215/65 R16 102T"})
    client.ad_ids_for = lambda ids, retry=True: {i["id"]: i["xml"] for i in client.active if i["id"] in ids and i["xml"]}
    applied = prebind(conn, tmp_path, results, client, unload("125"))
    assert [(e["code"], e["orphan"]) for e in applied[0][1]] == [("125", "106")] and applied[0][2] is True
    assert mirror_feed.manual_ads(conn, account) == [{"avito_id": 78, "title": "Kumho WinterCraft Ice Wi32 215/65 R16 102T"}]
    assert prebind(conn, tmp_path, results, client, unload("125")) == []                        # bound already and the same manual ad: nothing new


def test_a_tire_that_the_supplier_has_in_two_kinds_is_not_bound(tmp_path):
    conn, account = setup(tmp_path)
    results = built(conn, tmp_path, unload("125"))
    old_copy(conn, tmp_path, account, "106")
    both = catalog_xml(commodity_xml(NNOMMODIF="125", NREST="8", SMNFCODE="LOT-125", **SAME),
                       commodity_xml(NNOMMODIF="126", NREST="8", SMNFCODE="LOT-126", SMODIFNAME="Test Model шип", **SAME))      # studded and not: one title
    assert prebind(conn, tmp_path, results, cabinet_for(conn, tmp_path, account, "106"), both) == []
    assert prebind(conn, tmp_path, results, cabinet_for(conn, tmp_path, account, "106"), unload("999")) == []           # a code the unload does not know is not trusted
    assert mirror_feed.adopted_maps(conn, account)[0] == {}


def test_a_shared_code_and_an_id_that_is_taken_are_left_alone(tmp_path):
    conn, account = setup(tmp_path)
    results = built(conn, tmp_path, unload("125"))
    old_copy(conn, tmp_path, account, "106")
    parsed = parse_unload(unload("125"), "T1")
    client = cabinet_for(conn, tmp_path, account, "106")
    token = connections.get_account(conn, account)["feed_token"]
    row = connections.get_account(conn, account)
    assert cli._prebind_account(conn, row, client, tmp_path / "feed", parsed, taken=frozenset({"106"})) == []      # the Id was just taken by another account
    raw = json.dumps({"ids": {}, "images": {}, "shared": {"125": "2026-11-14"}})
    store.set_state(conn, mirror_feed._adopted_key(account), raw)
    assert cli._prebind_account(conn, row, client, tmp_path / "feed", parsed) == []                                  # agreed in advance to change on a date
    assert token


def test_the_report_calls_bound_only_what_is_really_in_the_new_feed(tmp_path, capsys):
    conn, account = setup(tmp_path)
    built(conn, tmp_path, unload("125"))
    row = connections.get_account(conn, account)
    binds = [{"code": "125", "orphan": "106", "title": "t"}, {"code": "126", "orphan": "NOT-IN-FEED", "title": "t"}]
    cli._prebind_report(conn, None, tmp_path / "feed", [(row, binds)])
    out = capsys.readouterr()
    assert "привязано до публикации: 0" not in out.out and "NOT-IN-FEED" in out.err and "NOT-IN-FEED" not in out.out


def test_the_cycle_builds_the_feeds_again_only_when_something_was_bound(tmp_path, monkeypatch):
    conn = prepared(tmp_path, with_second=False)
    connections.set_live(conn, 1, True, "2026-10-14T10:00:00", "o")
    conn.close()
    builds = []
    real = cli.run_for_accounts
    monkeypatch.setattr(cli, "run_for_accounts", lambda *a, **k: builds.append(1) or real(*a, **k))
    answers = iter([[], [({"id": 1, "name": "n", "feed_token": "none"}, [{"code": "1", "orphan": "2", "title": "t"}])], []])
    monkeypatch.setattr(cli, "_prebind_accounts", lambda *a, **k: next(answers))
    monkeypatch.setattr(cli, "_prebind_report", lambda *a, **k: None)
    main(["run-cycle", "--publish"] + files(tmp_path, ONE))
    assert len(builds) == 1                                                          # nothing bound: one build
    main(["run-cycle", "--publish"] + files(tmp_path, ONE))
    assert len(builds) == 3                                                          # bound: the second cycle built twice
    main(["run-cycle"] + files(tmp_path, ONE))                                       # a draft without publishing: no binding at all
    assert len(builds) == 4


def test_an_id_that_another_account_already_has_is_not_taken(tmp_path):
    conn, account = setup(tmp_path)
    other = connections.create_account(conn, "Второй", "2026-10-14T12:00:00", "o")
    connections.set_live(conn, other, True, "2026-10-14T12:00:00", "o")
    mirror_feed.bind_ids(conn, other, {"900": "106"}, "2026-10-15T10:00:00", "o")      # the ad "106" of the cabinet belongs to the other account
    conn.commit()
    results = built(conn, tmp_path, unload("125"))
    old_copy(conn, tmp_path, account, "106")
    row = connections.get_account(conn, account)
    assert cli._prebind_account(conn, row, cabinet_for(conn, tmp_path, account, "106"), tmp_path / "feed", parse_unload(unload("125"), "T1")) == []


def test_the_kind_of_the_old_ad_must_be_known_and_the_same(tmp_path):
    conn, account = setup(tmp_path)
    results = built(conn, tmp_path, unload("125"))
    client = cabinet_for(conn, tmp_path, account, "106")
    assert prebind(conn, tmp_path, results, client, unload("125")) == []                    # no copy of the old ad: not bound
    old_copy(conn, tmp_path, account, "106", runflat=True)                                  # the old ad was RunFlat, the new position is not
    assert prebind(conn, tmp_path, results, client, unload("125")) == []
    old_copy(conn, tmp_path, account, "106")                                                # the same kind
    assert len(prebind(conn, tmp_path, results, client, unload("125"))) == 1


def test_an_account_with_the_suffix_in_its_ids_binds_too(tmp_path):
    conn, first = setup(tmp_path)
    second = connections.create_account(conn, "Второй", "2026-10-14T12:00:00", "o")
    connections.set_live(conn, second, True, "2026-10-14T12:00:00", "o")
    conn.commit()
    results = built(conn, tmp_path, unload("125"))
    assert ids(tmp_path, conn, second) == ["125_a2"]
    old_copy(conn, tmp_path, second, "106_a2")
    applied = [x for x in prebind(conn, tmp_path, results, cabinet_for(conn, tmp_path, second, "106_a2"), unload("125")) if x[0]["id"] == second]
    assert applied and applied[0][1][0]["orphan"] == "106_a2"
    built(conn, tmp_path, unload("125"))
    assert ids(tmp_path, conn, second) == ["106_a2"]


def test_our_own_ad_given_without_its_id_and_an_ad_of_no_tire_are_not_manual(tmp_path):
    """The reviewer's cases: Avito leaves out the Id of one of OUR ads (it must not be taken off as «manual»); a rim or a service names no tire."""
    conn, account = setup(tmp_path)
    results = built(conn, tmp_path, unload("125"))
    token = connections.get_account(conn, account)["feed_token"]
    ours = ET.parse(tmp_path / "feed" / token / "next" / "avito_feed.xml").getroot().findall("Ad")[0].findtext("Id")
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, seen_at) VALUES (?,?,?,?)", (account, 90, ours, "2026-10-19T00:00:00"))
    conn.commit()
    client = Cabinet([{"id": 90, "xml": None, "title": "Kumho WinterCraft Ice Wi32 205/55 R16 94T"},
                      {"id": 91, "xml": None, "title": "Диски литые R16, 4 шт."}], [])
    client.ad_ids_for = lambda ids, retry=True: {}
    prebind(conn, tmp_path, results, client, unload("125"))
    assert mirror_feed.manual_ads(conn, account) == []
