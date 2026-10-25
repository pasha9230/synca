"""Owner, 2026-10-15: a new Id only for what Avito reports as deleted from the cabinet; an ad that is alive is never doubled."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from helpers import catalog_xml, commodity_xml
from test_reconcile import ad
from test_same_tire import NOW, ids, run, setup
from tires_sync import cli, heal, mirror_feed

DELETED = [{"messages": [{"title": "Не удалось обработать объявление, так как оно было удалено из Личного кабинета Авито.", "description": ""}]}]
TODAY = date(2026, 10, 15)


def item(ad_id, status="removed"):
    return dict(DELETED[0], ad_id=ad_id, avito_status=status)


def test_a_deleted_ad_with_no_live_twin_is_renewed_and_one_with_a_live_twin_takes_its_id():
    feed = {"A_a2": ad("A_a2"), "B_a2": ad("B_a2", width="195"), "C_a2": ad("C_a2", width="175")}
    cabinet = [{"ad_id": "OLD-A", "status": "active", "title": "Kumho WinterCraft Ice Wi32 235/55 R19 105T"}]
    result = heal.plan(feed, [item("A_a2"), item("B_a2")], cabinet, {}, {}, TODAY)
    assert [(b["orphan"], b["code"]) for b in result["bind"]] == [("OLD-A", "A")]                # A: the live twin is taken
    assert result["renew"] == ["B"] and result["repeat"] == []                                   # B: nothing alive, a new Id


def test_a_position_is_not_renewed_twice_within_a_week_and_the_batch_is_limited():
    feed = {"%d_a2" % i: ad("%d_a2" % i, width=str(100 + i)) for i in range(30)}
    reports = [item("%d_a2" % i) for i in range(30)]
    result = heal.plan(feed, reports, [], {}, {"0": "r20261012"}, TODAY, limit=5)
    assert result["repeat"] == ["0"] and len(result["renew"]) == 5 and len(result["over_limit"]) == 24
    assert heal.plan(feed, reports[:1], [], {}, {"0": "r20261001"}, TODAY)["renew"] == ["0"]     # long ago: allowed again


def test_an_id_that_is_not_in_the_feed_is_left_alone():
    assert heal.plan({"X": ad("X")}, [item("GONE")], [], {}, {}, TODAY) == {"bind": [], "renew": [], "repeat": [], "over_limit": [], "recent": [], "alive": []}


class FakeClient:
    def __init__(self, active, report):
        self.active, self.report = active, report

    def items(self, status="active", per_page=100):
        return iter(self.active if status == "active" else [])

    def ad_ids_for(self, ids):
        return {i["id"]: i["xml"] for i in self.active if i["id"] in ids}

    def upload_items(self, kind="current"):
        return iter(self.report)


def test_the_cycle_hook_binds_to_the_live_ad_and_only_proposes_a_new_id(tmp_path):
    from tires_sync import connections, publishing
    conn, account = setup(tmp_path)
    unload = catalog_xml(commodity_xml(NNOMMODIF="106", NREST="8", SMNFCODE="LOT-A", SLOAD="94", SWIDTH="205"),
                         commodity_xml(NNOMMODIF="200", NREST="9", SMNFCODE="O", SLOAD="91", SWIDTH="195"))
    run(conn, tmp_path, unload)
    token = connections.get_account(conn, account)["feed_token"]
    shown = ET.parse(tmp_path / "feed" / token / "avito_feed.xml").getroot().findall("Ad")
    one = [a for a in shown if a.findtext("TireSectionWidth") == "205"][0]
    two = [a for a in shown if a.findtext("TireSectionWidth") == "195"][0]
    title = "%s %s 205/55 R16 94%s" % (one.findtext("Brand"), one.findtext("Model"), one.findtext("SpeedIndex"))
    client = FakeClient([{"id": 77, "xml": "OLD-106", "title": title}],
                        [item(one.findtext("Id")), item(two.findtext("Id"))])
    result = cli._heal_account(conn, connections.get_account(conn, account), client, tmp_path / "feed", True)
    assert [b["code"] for b in result["bind"]] == ["106"] and result["renew"] == ["200"]
    assert mirror_feed.adopted_maps(conn, account)[0]["106"] == "OLD-106"
    assert mirror_feed.reissued_own(conn, account).get("200") is None                    # owner, 2026-10-19: the cycle never gives a new Id
    cli._heal_account(conn, connections.get_account(conn, account), client, tmp_path / "feed", True, renew=True)   # only by his word
    assert mirror_feed.reissued_own(conn, account).get("200") == "r" + cli.moscow_now().strftime("%Y%m%d")


def test_a_position_that_came_back_to_the_feed_lately_keeps_its_id_while_a_stale_report_calls_it_deleted():
    """Owner, 2026-10-15: what returns from the archive must come back under the same Id: a report written while the ad was off the feed
    is no reason for a new Id."""
    feed = {"A_a2": ad("A_a2"), "B_a2": ad("B_a2", width="195")}
    result = heal.plan(feed, [item("A_a2"), item("B_a2")], [], {}, {}, TODAY, recent={"A"})
    assert result["renew"] == ["B"] and result["recent"] == ["A"]                                # A waits, B is renewed as before


def test_the_cycle_hook_does_not_renew_a_position_that_was_removed_and_published_again_lately(tmp_path):
    from tires_sync import connections, store
    conn, account = setup(tmp_path)
    run(conn, tmp_path, catalog_xml(commodity_xml(NNOMMODIF="200", NREST="9", SMNFCODE="O", SLOAD="91", SWIDTH="195")))
    token = connections.get_account(conn, account)["feed_token"]
    one = ET.parse(tmp_path / "feed" / token / "avito_feed.xml").getroot().findall("Ad")[0]
    client = FakeClient([], [item(one.findtext("Id"))])
    acct = connections.get_account(conn, account)
    store.record_event(conn, "2000-01-01T00:00:00", account, "200", "", "waiting", "cart_wait:absent_from_supplier")
    assert cli._heal_account(conn, acct, client, tmp_path / "feed", False)["renew"] == ["200"]   # waited once, never removed: as before
    store.record_event(conn, "2000-01-01T00:00:00", account, "200", "", "removed", "absent_from_supplier")
    result = cli._heal_account(conn, acct, client, tmp_path / "feed", True)                      # removed, then published again now
    assert result["renew"] == [] and result["recent"] == ["200"]
    assert mirror_feed.reissued_own(conn, account).get("200") is None                           # no new Id was written
    conn.execute("UPDATE ad_event SET at = '2000-01-02T00:00:00' WHERE goods_id = '200' AND event = 'published'")
    assert cli._heal_account(conn, acct, client, tmp_path / "feed", False)["renew"] == ["200"]   # long ago: a deleted ad is renewed


def test_a_code_with_an_ad_alive_in_the_cabinet_is_never_renewed():
    """The reviewer's case: the set of four of a position is reported deleted while its single ad is alive; renewing the code would give the live
    single ad a new Id too (and Avito blocks it as a repeat)."""
    feed = {"200-x4_a2": ad("200-x4_a2", width="195"), "200_a2": ad("200_a2", width="195"), "300_a2": ad("300_a2", width="175")}
    cabinet = [{"ad_id": "200_a2", "status": "active", "title": "Other 1 2/3 R4 5T"}]
    result = heal.plan(feed, [item("200-x4_a2"), item("300_a2")], cabinet, {}, {}, TODAY)
    assert result["alive"] == ["200"] and result["renew"] == ["300"]
    blocked = [{"ad_id": "200_a2", "status": "blocked", "title": "Other 1 2/3 R4 5T"}]            # a blocked ad is not alive
    assert heal.plan(feed, [item("200-x4_a2")], blocked, {}, {}, TODAY)["renew"] == ["200"]


def test_the_window_of_a_return_is_counted_in_days_and_the_order_matters(tmp_path):
    from datetime import timedelta
    from tires_sync import connections, store
    conn, account = setup(tmp_path)
    store.record_event(conn, "2026-10-10T08:00:00", account, "200", "", "removed")
    store.record_event(conn, "2026-10-13T08:00:00", account, "200", "", "published")
    assert cli._recently_back(conn, account, TODAY) == {"200"}                                    # the 15th: the cutoff is the 12th, inside
    assert cli._recently_back(conn, account, TODAY + timedelta(days=1)) == {"200"}                # the 16th: the cutoff is the 13th, still inside
    assert cli._recently_back(conn, account, TODAY + timedelta(days=2)) == set()                  # the 17th: the cutoff is the 14th, outside
    store.record_event(conn, "2026-10-14T08:00:00", account, "300", "", "published")
    store.record_event(conn, "2026-10-14T09:00:00", account, "300", "", "removed")                 # removed AFTER the publication: not a return
    assert "300" not in cli._recently_back(conn, account, TODAY)


def test_the_description_names_the_codes_that_wait_for_a_human():
    plan = {"bind": [], "renew": [], "repeat": [], "over_limit": [], "recent": ["7"], "alive": ["200"]}
    text = cli._describe_heal({"name": "A"}, plan)
    assert "ручной разбор" in text and "200" in text and "вернулись из архива" in text and "7" in text


def _alerts_of(monkeypatch, tmp_path, name, **found):
    """What `_heal_accounts` tells the owner when `_heal_account` finds `found` (the other keys are empty)."""
    from tires_sync import connections
    folder = tmp_path / name
    folder.mkdir()
    conn, account = setup(folder)
    result = {"bind": [], "renew": [], "repeat": [], "over_limit": [], "recent": [], "alive": []}
    result.update(found)
    sent = []
    monkeypatch.setattr(connections, "get_secret", lambda *a, **k: "x")
    monkeypatch.setattr(cli, "AvitoClient", lambda *a, **k: object())
    calls = []
    monkeypatch.setattr(cli, "_heal_account", lambda *a, **k: calls.append((a, k)) or result)
    monkeypatch.setattr(cli, "_alert", lambda conn, box, text, **k: sent.append(text) or True)
    cli._heal_accounts(conn, object(), folder / "feed")
    assert calls and all(not k.get("renew") and a[5:6] != (True,) for a, k in calls)          # the cycle never gives a new Id itself
    return sent


def test_the_alert_says_what_was_done_and_a_bare_alive_asks_for_a_human_instead_of_claiming_a_fix(monkeypatch, tmp_path):
    proposed = _alerts_of(monkeypatch, tmp_path, "fixed", renew=["200"])                                  # a new Id is only proposed (2026-10-19)
    assert len(proposed) == 1 and "ручной разбор" in proposed[0].split("\n")[0] and "по слову владельца" in proposed[0]
    waiting = _alerts_of(monkeypatch, tmp_path, "waiting", alive=["200"])
    assert len(waiting) == 1 and "ручной разбор" in waiting[0].split("\n")[0] and "исправлены" not in waiting[0].split("\n")[0]
    for name, found in (("repeat", {"repeat": ["5"]}), ("limit", {"over_limit": ["6"]})):                 # nothing is written for these either
        only = _alerts_of(monkeypatch, tmp_path, name, **found)
        assert len(only) == 1 and "ручной разбор" in only[0].split("\n")[0]
    bound = _alerts_of(monkeypatch, tmp_path, "bound", bind=[{"orphan": "A", "feed_id": "B", "code": "3", "title": "t"}])
    assert bound[0].startswith("Tires Sync: исправлены Id")                                              # a bind is written to our data
    mixed = _alerts_of(monkeypatch, tmp_path, "mixed", renew=["1"], alive=["200"])
    assert "ручной разбор" in mixed[0].split("\n")[0]                                       # a proposed new Id changes nothing in our data
    both = _alerts_of(monkeypatch, tmp_path, "both", renew=["1"], bind=[{"orphan": "A", "feed_id": "B", "code": "3", "title": "t"}])
    assert both[0].startswith("Tires Sync: исправлены Id") and "позиция 3 «t»: Id в фиде B -> Id живого объявления A" in both[0]


def test_a_bare_return_from_the_archive_sends_nothing(monkeypatch, tmp_path):
    assert _alerts_of(monkeypatch, tmp_path, "back", recent=["7"]) == []


def test_a_new_id_is_taken_back_by_ids_unrenew_only_with_confirm(tmp_path, capsys):
    """Owner, 2026-10-19: the Kumho of «Партнёр-2» that the cycle renewed goes back to the Id it had."""
    conn, account = setup(tmp_path)
    mirror_feed.reissue_own(conn, account, ["125452764813", "300"], "20261019", NOW, "ids-heal")
    conn.commit()
    argv = ["ids-unrenew", "--db", str(tmp_path / "t.db"), "--account", str(account), "--codes", "125452764813,999"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "125452764813" in out and "r20261019" in out and "999" in out and "Ничего не изменено" in out
    assert "125452764813" in mirror_feed.reissued_own(conn, account)
    assert cli.main(argv + ["--confirm"]) == 0
    conn = cli.store.connect(tmp_path / "t.db")
    assert mirror_feed.reissued_own(conn, account) == {"300": "r20261019"}                    # only the named code goes back


def test_a_proposed_new_id_is_told_once_not_every_cycle(monkeypatch, tmp_path):
    from tires_sync import connections
    conn, account = setup(tmp_path)
    found = {"bind": [], "renew": ["200"], "repeat": [], "over_limit": [], "recent": [], "alive": []}
    sent = []
    monkeypatch.setattr(connections, "get_secret", lambda *a, **k: "x")
    monkeypatch.setattr(cli, "AvitoClient", lambda *a, **k: object())
    monkeypatch.setattr(cli, "_heal_account", lambda *a, **k: dict(found))
    monkeypatch.setattr(cli, "_alert", lambda conn, box, text, **k: sent.append(text) or True)
    cli._heal_accounts(conn, object(), tmp_path / "feed")
    cli._heal_accounts(conn, object(), tmp_path / "feed")
    assert len(sent) == 1                                                                         # the same proposal: once
    found["renew"] = ["200", "300"]
    cli._heal_accounts(conn, object(), tmp_path / "feed")
    assert len(sent) == 2 and "300" in sent[1]                                                    # a new one: told again


def test_a_bare_removed_status_is_not_a_deletion_from_the_cabinet():
    """Owner, 2026-10-19: while the Id is in the cabinet in any status (archived, taken off), no new Id; only «удалено из Личного кабинета» is."""
    feed = {"A_a2": ad("A_a2"), "B_a2": ad("B_a2", width="195")}
    taken_off = {"ad_id": "A_a2", "avito_status": "removed", "messages": [{"title": "Объявление снято с публикации", "description": ""}]}
    assert heal.deleted_ids([taken_off, item("B_a2")]) == ["B_a2"]
    assert heal.plan(feed, [taken_off, item("B_a2")], [], {}, {}, TODAY)["renew"] == ["B"]
