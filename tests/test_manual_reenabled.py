"""Owner, 2026-10-22 («Партнёр-2 — да не снимай их»): a live ad of the cabinet of «Партнёр-2» whose Id from the file our published feed no longer
has, and which stays alive after Avito took a file without it, was switched on by hand: it is a manual ad (the feed neither takes it off, nor takes
it over, nor puts the same tire next to it) until it is gone."""
from __future__ import annotations

import json
from datetime import datetime

from helpers import catalog_xml, commodity_xml
from http_fakes import ScriptedHttp
from test_avito_report import TOKEN
from test_prebind import Cabinet, built, old_copy, prebind
from test_same_tire import ids, run, setup
from tires_sync import avito_report, cli, connections, mirror_feed, store
from tires_sync.avito import AvitoClient
from tires_sync.webmim import parse_unload

BEFORE, AFTER = "2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z"     # the last upload of Avito started before / after the ad was seen out of our file


def unload(stock=8):
    return catalog_xml(commodity_xml(NNOMMODIF="106", NREST=str(stock), SMNFCODE="LOT-A", SLOAD="94", SWIDTH="205"),
                       commodity_xml(NNOMMODIF="200", NREST="9", SMNFCODE="O", SLOAD="91", SWIDTH="195"))


def two_accounts(tmp_path):
    conn, first = setup(tmp_path)
    second = connections.create_account(conn, "Партнёр-2", "2026-10-14T12:00:00", "o")
    connections.set_live(conn, second, True, "2026-10-14T12:00:00", "o")
    conn.commit()
    assert second == 2
    return conn, first, second


def uploaded(conn, account, started):
    store.set_state(conn, avito_report.KEY % account, json.dumps({"last_started_at": started}))


def cabinet(*ads):
    """[(avito_id, Id from the file, title)] of the live ads."""
    client = Cabinet([{"id": a, "xml": x, "title": t} for a, x, t in ads], [])
    client.ad_ids_for = lambda wanted, retry=True: {i["id"]: i["xml"] for i in client.active if i["id"] in wanted and i["xml"]}
    return client


def look(conn, tmp_path, account, client, xml):
    row = connections.get_account(conn, account)
    return cli._prebind_account(conn, row, client, tmp_path / "feed", parse_unload(xml, "T1"), changed=[])


TITLE = "Test Brand Test Model 205/55 R16 94T"


def test_an_ad_switched_on_by_hand_after_avito_took_it_off_is_manual_and_its_tire_is_not_taken_back(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    mirror_feed.bind_ids(conn, second, {"106": "OLD-106"}, "2026-10-14T12:00:00", "o")         # moved from the mirror: the position has the old Id
    conn.commit()
    run(conn, tmp_path, unload())
    assert ids(tmp_path, conn, second) == ["200_a2", "OLD-106"]
    run(conn, tmp_path, unload(stock=2), day=15)                                               # fewer than four: taken off our feed
    assert ids(tmp_path, conn, second) == ["200_a2"]
    client = cabinet((1, "200_a2", "x"), (50, "OLD-106", "Шина старая, как есть"))             # switched on by hand; a title of no tire
    uploaded(conn, second, BEFORE)
    look(conn, tmp_path, second, client, unload())
    assert mirror_feed.manual_ads(conn, second) == []                                          # Avito has not taken a file without it yet: ours, just taken off
    uploaded(conn, second, AFTER)
    look(conn, tmp_path, second, client, unload())
    assert mirror_feed.manual_ads(conn, second) == [{"avito_id": 50, "title": "Шина старая, как есть", "ad_id": "OLD-106", "code": "106"}]
    run(conn, tmp_path, unload(), day=16)                                                      # the stock is back
    assert ids(tmp_path, conn, second) == ["200_a2"]                                           # not taken over, the same tire not next to it
    assert ids(tmp_path, conn, first) == ["106", "200"]                                        # the other account is not touched
    look(conn, tmp_path, second, cabinet((1, "200_a2", "x")), unload())                        # taken off by hand or expired
    assert mirror_feed.manual_ads(conn, second) == []
    run(conn, tmp_path, unload(), day=17)
    assert ids(tmp_path, conn, second) == ["200_a2", "OLD-106"]                                # back by the usual rules, under its Id


def test_our_published_ad_of_the_same_tire_stays_next_to_an_ad_switched_on_by_hand(tmp_path):
    """Team lead, 2026-10-22: one ad less on Avito would make no sense; «not next to it» is for a new publication only."""
    conn, first, second = two_accounts(tmp_path)
    run(conn, tmp_path, unload())
    assert ids(tmp_path, conn, second) == ["106_a2", "200_a2"]
    title = TITLE.replace("Test Brand Test Model", _brand_model(tmp_path, conn, second))
    client = cabinet((1, "106_a2", "x"), (2, "200_a2", "y"), (60, "125452769892_old", title))     # an old ad of another file, the same tire
    look(conn, tmp_path, second, client, unload())
    uploaded(conn, second, AFTER)
    look(conn, tmp_path, second, client, unload())
    assert [a["ad_id"] for a in mirror_feed.manual_ads(conn, second)] == ["125452769892_old"]
    run(conn, tmp_path, unload(), day=15)
    assert ids(tmp_path, conn, second) == ["106_a2", "200_a2"]


def test_the_same_tire_is_not_published_anew_next_to_an_ad_switched_on_by_hand(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    run(conn, tmp_path, unload())
    title = TITLE.replace("Test Brand Test Model", _brand_model(tmp_path, conn, second))
    run(conn, tmp_path, unload(stock=2), day=15)                                               # ours taken off
    assert ids(tmp_path, conn, second) == ["200_a2"]
    client = cabinet((2, "200_a2", "y"), (60, "125452769892_old", title))
    look(conn, tmp_path, second, client, unload())
    uploaded(conn, second, AFTER)
    look(conn, tmp_path, second, client, unload())
    run(conn, tmp_path, unload(), day=16)                                                      # the stock is back
    assert ids(tmp_path, conn, second) == ["200_a2"]
    reasons = [r[0] for r in conn.execute("SELECT reason FROM ad_event WHERE account_id = ? AND goods_id = '106' AND event = 'removed'", (second,))]
    assert "manual_ad" not in reasons                                                          # skipped, never taken off for it
    assert ids(tmp_path, conn, first) == ["106", "200"]


def test_an_answer_of_avito_without_the_id_keeps_the_ad_manual(tmp_path):
    """Opus review: Avito sometimes leaves an Id out; the ad must not fall out of the manual ones and be taken over by the position of its code."""
    conn, first, second = two_accounts(tmp_path)
    mirror_feed.bind_ids(conn, second, {"106": "OLD-106"}, "2026-10-14T12:00:00", "o")
    conn.commit()
    run(conn, tmp_path, unload(stock=2))
    client = cabinet((1, "200_a2", "x"), (50, "OLD-106", TITLE))
    look(conn, tmp_path, second, client, unload())
    uploaded(conn, second, AFTER)
    look(conn, tmp_path, second, client, unload())
    client.ad_ids_for = lambda wanted, retry=True: {1: "200_a2"}                               # this time without the Id of the ad 50
    look(conn, tmp_path, second, client, unload())
    assert mirror_feed.manual_ads(conn, second) == [{"avito_id": 50, "title": TITLE, "ad_id": "OLD-106", "code": "106"}]     # once, not also as one without an Id
    run(conn, tmp_path, unload(), day=15)
    assert ids(tmp_path, conn, second) == ["200_a2"]


def test_an_upload_before_our_feed_last_took_the_id_off_does_not_confirm(tmp_path, monkeypatch):
    """Opus review: the Id may have been back in a file published between two looks (a look skipped, a publication from the panel); only an
    upload after our feed last took it off proves Avito took a file without it."""
    conn, first, second = two_accounts(tmp_path)
    run(conn, tmp_path, unload())
    monkeypatch.setattr(cli, "moscow_now", lambda: datetime(2026, 10, 22, 10, 0, 0))
    client = cabinet((1, "106_a2", "x"), (2, "200_a2", "y"), (70, "OLD-1", TITLE), (71, "125452769892_old", "Шина старая"))
    look(conn, tmp_path, second, client, unload())                                             # first seen at 10:00
    for goods, ad_id in (("OLD-1", "OLD-1"), ("125452769892", "125452769892_a2")):           # back and taken off again at 12:00 (by Id; by the position)
        conn.execute("INSERT OR REPLACE INTO retired_ad (account_id, goods_id, ad_id, identity, removed_at) VALUES (?,?,?,?,?)",
                     (second, goods, ad_id, "[]", "2026-10-22T12:00:00"))
    conn.commit()
    uploaded(conn, second, "2026-10-22T11:00:00+03:00")                                        # after the first sight, before the last taking off
    look(conn, tmp_path, second, client, unload())
    confirmed = lambda: sorted(k for k, e in mirror_feed.stray_seen(conn, second).items() if e.get("confirmed"))
    assert mirror_feed.manual_ads(conn, second) == [] and confirmed() == []
    uploaded(conn, second, "2026-10-22T12:30:00+03:00")
    look(conn, tmp_path, second, client, unload())
    assert confirmed() == ["70", "71"]
    assert [a["ad_id"] for a in mirror_feed.manual_ads(conn, second)] == ["OLD-1"]             # 71 names no tire and is none of our positions


def test_in_the_order_of_the_cycle_an_upload_between_two_publications_confirms(tmp_path, monkeypatch):
    """Opus review, round 2: publication, report, the look of the next cycle. The upload Avito made between two of our publications is what
    the report of the cycle keeps; it must confirm an ad seen before it (a check against the time of our file never would)."""
    from datetime import timedelta
    conn, first, second = two_accounts(tmp_path)
    run(conn, tmp_path, unload())
    now = datetime.fromisoformat(store.moscow_iso())
    client = cabinet((1, "106_a2", "x"), (2, "200_a2", "y"), (70, "OLD-1", TITLE))
    monkeypatch.setattr(cli, "moscow_now", lambda: now - timedelta(hours=2))
    look(conn, tmp_path, second, client, unload())                                             # cycle k: first seen
    run(conn, tmp_path, unload(), day=15)                                                      # cycle k: published
    uploaded(conn, second, (now - timedelta(hours=1)).isoformat())                            # Avito took it an hour later; the next report keeps it
    run(conn, tmp_path, unload(), day=16)                                                      # cycle k+1 published again (the file is new)
    monkeypatch.setattr(cli, "moscow_now", lambda: now)
    look(conn, tmp_path, second, client, unload())                                             # the look of cycle k+2
    assert [a["ad_id"] for a in mirror_feed.manual_ads(conn, second)] == ["OLD-1"]


def test_a_lost_published_file_keeps_the_manual_ads(tmp_path, capsys):
    conn, first, second = two_accounts(tmp_path)
    run(conn, tmp_path, unload())
    client = cabinet((1, "106_a2", "x"), (2, "200_a2", "y"), (70, "OLD-1", TITLE))
    look(conn, tmp_path, second, client, unload())
    uploaded(conn, second, AFTER)
    look(conn, tmp_path, second, client, unload())
    token = connections.get_account(conn, second)["feed_token"]
    (tmp_path / "feed" / token / "avito_feed.xml").unlink()
    cli._prebind_accounts(conn, None, tmp_path / "feed", [_ok(second)], parsed=parse_unload(unload(), "T1"), client_for=lambda acc: client)
    assert "привязка Id до публикации не выполнена" in capsys.readouterr().err
    assert [a["ad_id"] for a in mirror_feed.manual_ads(conn, second)] == ["OLD-1"]


def test_the_heal_after_publishing_does_not_take_over_an_ad_switched_on_by_hand(tmp_path):
    from test_heal import FakeClient
    conn, first, second = two_accounts(tmp_path)
    run(conn, tmp_path, unload())
    title = TITLE.replace("Test Brand Test Model", _brand_model(tmp_path, conn, second))
    old_copy(conn, tmp_path, second, "OLD-1")
    mirror_feed.set_manual_ads(conn, second, [{"avito_id": 70, "title": title, "ad_id": "OLD-1", "code": "OLD-1"}])
    client = FakeClient([{"id": 70, "xml": "OLD-1", "title": title}], [])                     # our 106_a2 is not live (blocked), the manual one is
    result = cli._heal_account(conn, connections.get_account(conn, second), client, tmp_path / "feed", True)
    assert result["bind"] == [] and mirror_feed.adopted_maps(conn, second)[0] == {}
    mirror_feed.set_manual_ads(conn, second, [])                                               # the list kept from before (the picture not trusted)...
    store.set_state(conn, "manual_stray_2", json.dumps({"70": {"ad_id": "OLD-1", "since": "2026-10-22T10:00:00", "confirmed": True}}))
    result = cli._heal_account(conn, connections.get_account(conn, second), client, tmp_path / "feed", True)
    assert result["bind"] == []                                                                # ...but the ad is confirmed: not taken over either


def _brand_model(tmp_path, conn, account):
    import xml.etree.ElementTree as ET
    token = connections.get_account(conn, account)["feed_token"]
    ad = [a for a in ET.parse(tmp_path / "feed" / token / "avito_feed.xml").getroot().findall("Ad") if a.findtext("TireSectionWidth") == "205"][0]
    assert ad.findtext("SpeedIndex") == "T"
    return "%s %s" % (ad.findtext("Brand"), ad.findtext("Model"))


def test_a_later_upload_confirms_it_once_and_for_as_long_as_it_lives(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    run(conn, tmp_path, unload())
    client = cabinet((1, "106_a2", "x"), (2, "200_a2", "y"), (70, "OLD-1", TITLE))
    uploaded(conn, second, BEFORE)
    look(conn, tmp_path, second, client, unload())
    assert "70" in json.loads(store.get_state(conn, "manual_stray_2")) and mirror_feed.manual_ads(conn, second) == []
    uploaded(conn, second, AFTER)
    look(conn, tmp_path, second, client, unload())
    assert [a["ad_id"] for a in mirror_feed.manual_ads(conn, second)] == ["OLD-1"]
    store.set_state(conn, avito_report.KEY % second, "{}")                                     # the time of the upload is not known any more
    look(conn, tmp_path, second, client, unload())
    assert [a["ad_id"] for a in mirror_feed.manual_ads(conn, second)] == ["OLD-1"]             # once confirmed, it stays while it lives out of our file


def test_an_upload_before_the_ad_was_seen_does_not_confirm_it(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    run(conn, tmp_path, unload())
    client = cabinet((1, "106_a2", "x"), (2, "200_a2", "y"), (70, "OLD-1", TITLE))
    uploaded(conn, second, BEFORE)
    for _ in range(3):
        look(conn, tmp_path, second, client, unload())
    assert mirror_feed.manual_ads(conn, second) == []
    store.set_state(conn, avito_report.KEY % second, "{}")
    look(conn, tmp_path, second, client, unload())
    assert mirror_feed.manual_ads(conn, second) == []


def test_an_id_back_in_our_published_file_starts_the_wait_again(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    run(conn, tmp_path, unload())
    client = cabinet((1, "106_a2", "x"), (2, "200_a2", "y"), (70, "OLD-1", TITLE))
    look(conn, tmp_path, second, client, unload())
    assert "70" in json.loads(store.get_state(conn, "manual_stray_2"))
    look(conn, tmp_path, second, cabinet((1, "106_a2", "x"), (2, "200_a2", "y")), unload())     # gone from the live ads: forgotten
    assert json.loads(store.get_state(conn, "manual_stray_2")) == {}
    look(conn, tmp_path, second, cabinet((1, "106_a2", "x"), (2, "200_a2", "y"), (3, "200_a2", "z")), unload())
    assert json.loads(store.get_state(conn, "manual_stray_2")) == {}                           # an Id of our published file is never a stray


def test_the_new_code_does_not_take_over_an_ad_switched_on_by_hand(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    xml = catalog_xml(commodity_xml(NNOMMODIF="125", NREST="8", SMNFCODE="LOT-125", SLOAD="94", SWIDTH="205"))
    results = built(conn, tmp_path, xml)
    from tires_sync import publishing
    publishing.publish_live_accounts(conn, tmp_path / "feed")
    title = TITLE.replace("Test Brand Test Model", _brand_model(tmp_path, conn, second))
    client = cabinet((77, "106_a2", title))                                                    # the old ad of the tire lives; the new code is not out yet
    look(conn, tmp_path, second, client, xml)                                                  # first sight (no copy of the old ad: nothing bound)
    old_copy(conn, tmp_path, second, "106_a2")
    uploaded(conn, second, AFTER)
    assert all(not b for _a, b, *_ in prebind(conn, tmp_path, results, client, xml))
    assert mirror_feed.adopted_maps(conn, second)[0] == {}                                     # not taken over
    assert [a["ad_id"] for a in mirror_feed.manual_ads(conn, second)] == ["106_a2"]


def test_another_account_keeps_the_old_rules(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    run(conn, tmp_path, unload())
    title = TITLE.replace("Test Brand Test Model", _brand_model(tmp_path, conn, first))
    client = cabinet((1, "106", "x"), (2, "200", "y"), (80, "OLD-9", title))
    uploaded(conn, first, AFTER)
    look(conn, tmp_path, first, client, unload())
    look(conn, tmp_path, first, client, unload())
    assert mirror_feed.manual_ads(conn, first) == [] and store.get_state(conn, "manual_stray_1") is None
    run(conn, tmp_path, unload(), day=15)
    assert ids(tmp_path, conn, first) == ["106", "200"]


def test_too_many_at_once_is_not_trusted(tmp_path, capsys, monkeypatch):
    told = []
    monkeypatch.setattr(cli, "_alert", lambda conn, box, text, **kw: told.append(text) or True)     # the owner is told, not only the log
    conn, first, second = two_accounts(tmp_path)
    run(conn, tmp_path, unload())
    many = [(100 + i, "OLD-%d" % i, "Brand%d Model 205/55 R16 94T" % i) for i in range(cli.REENABLED_LIMIT + 1)]
    client = cabinet((1, "106_a2", "x"), (2, "200_a2", "y"), *many)
    look(conn, tmp_path, second, client, unload())
    uploaded(conn, second, AFTER)
    applied = cli._prebind_accounts(conn, None, tmp_path / "feed", [r for r in [_ok(second)]], parsed=parse_unload(unload(), "T1"),
                                    client_for=lambda acc: client)
    assert applied == [] and "включены вручную" in capsys.readouterr().err
    assert len(told) == 1 and "Партнёр-2" in told[0] and "включены вручную" in told[0]
    assert mirror_feed.manual_ads(conn, second) == []


def _ok(account):
    from types import SimpleNamespace
    return SimpleNamespace(ok=True, account_id=account)


def test_the_time_of_the_upload_is_read_as_moscow_time():
    parse = avito_report.started_moscow
    assert parse("2026-10-12T11:22:59Z") == datetime(2026, 10, 12, 14, 22, 59)
    assert parse("2026-10-12T14:22:59+03:00") == datetime(2026, 10, 12, 14, 22, 59)
    assert parse("2026-10-12T14:22:59") == datetime(2026, 10, 12, 14, 22, 59)                # no zone: the earliest reading (Moscow)
    assert parse("") is None and parse(None) is None and parse("вчера") is None


def test_collect_keeps_the_start_of_the_last_successful_upload(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    http = ScriptedHttp(
        TOKEN, (200, {"upload_id": 9, "status": "success", "started_at": "2026-10-12T11:22:59Z", "stats": {"count": 0}}),
        (200, {"upload_id": 10, "status": "processing", "started_at": "2026-10-12T15:00:00Z"}),
        (200, {"items": [], "meta": {"pages": 1}}))
    client = AvitoClient("id", "secret", http=http, now=lambda: 0.0, sleep=lambda s: None)
    avito_report.collect(conn, client, 2, "2026-10-12T15:00:00")
    assert avito_report.last_started(conn, 2) == datetime(2026, 10, 12, 14, 22, 59)          # the successful one, not the running one


def test_an_account_with_nothing_published_yet_has_none(tmp_path):
    conn, first, second = two_accounts(tmp_path)
    built(conn, tmp_path, unload())                                                            # a draft only: Avito never took a file of ours
    uploaded(conn, second, AFTER)
    look(conn, tmp_path, second, cabinet((70, "OLD-1", TITLE)), unload())
    assert mirror_feed.manual_ads(conn, second) == [] and store.get_state(conn, "manual_stray_2") is None
