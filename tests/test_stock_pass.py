"""`run-cycle --stock-only`: the light pass between the hourly cycles (owner, 2026-10-24, «вариант Б»). Only the positions whose stock fell below
the threshold or that left the unload leave the feed; every other ad stays as it was published, nothing new comes in."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from helpers import catalog_xml, commodity_xml, settings_dict
from tires_sync import connections, cycle, feed_check, publishing, store
from tires_sync.cli import main
from tires_sync.secretbox import create_key_file, load_box
from tires_sync.settings_store import init_settings

NOW = "2026-10-24T10:00:00"
REF = '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>'


def rows(*items) -> bytes:
    return catalog_xml(*(commodity_xml(TERRITORY_RN="T1", **kw) for kw in items))


FIVE = rows(*(dict(NNOMMODIF=str(n)) for n in range(101, 106)))


def prepared(tmp_path, accounts=("A", "B"), **settings):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(**settings), NOW)
    for name in accounts:
        connections.set_live(conn, connections.create_account(conn, name, NOW, "owner"), True, NOW, "owner")
    (tmp_path / "reference.txt").write_text(REF, encoding="utf-8")
    conn.close()


def args_of(tmp_path):
    return ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
            "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed")]


def run(tmp_path, unload, *extra) -> int:
    if unload is not None:
        (tmp_path / "unload.xml").write_bytes(unload)
    return main(["run-cycle", "--publish"] + list(extra) + args_of(tmp_path))


def token(tmp_path, account_id):
    conn = store.connect(tmp_path / "t.db")
    try:
        return connections.get_account(conn, account_id)["feed_token"]
    finally:
        conn.close()


def public(tmp_path, account_id=1) -> bytes:
    return (tmp_path / "feed" / token(tmp_path, account_id) / "avito_feed.xml").read_bytes()


def ads_of(data: bytes) -> dict:
    return {a.findtext("Id"): ET.tostring(a) for a in ET.fromstring(data).findall("Ad")}


def published(tmp_path, account_id=1):
    conn = store.connect(tmp_path / "t.db")
    try:
        return store.load_published(conn, account_id)
    finally:
        conn.close()


def events(tmp_path, account_id=1):
    conn = store.connect(tmp_path / "t.db")
    try:
        return [tuple(r) for r in conn.execute("SELECT goods_id, event, reason FROM ad_event WHERE account_id = ? AND event = 'removed' ORDER BY id",
                                               (account_id,))]
    finally:
        conn.close()


def alerts(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    try:
        return [json.loads(r[0])["text"] for r in conn.execute("SELECT detail FROM audit WHERE action = 'alert' ORDER BY id")]
    finally:
        conn.close()


AFTER = rows(dict(NNOMMODIF="101", NREST="2"),                                                     # fell below the threshold
             dict(NNOMMODIF="103", NPRICE_DELAYED="6000", SMODIFNAME="IKON 205/55/16 T 96 Другое имя"),   # other price and name
             dict(NNOMMODIF="104", SPICTURE="https://example.test/new.jpg"),                       # another photo
             dict(NNOMMODIF="105"), dict(NNOMMODIF="106"))                                         # 102 left; 106 is new


def test_the_pass_takes_off_only_the_fallen_and_the_gone_and_leaves_every_other_ad_as_published(tmp_path, capsys):
    prepared(tmp_path)
    assert run(tmp_path, FIVE) == 0
    before = {1: ads_of(public(tmp_path, 1)), 2: ads_of(public(tmp_path, 2))}
    assert set(before[1]) == {"101", "102", "103", "104", "105"} and set(before[2]) == {"101_a2", "102_a2", "103_a2", "104_a2", "105_a2"}
    assert run(tmp_path, AFTER, "--stock-only") == 0
    out = capsys.readouterr().out
    assert "A: сняты по остатку (2): 101, 102" in out and "B: сняты по остатку (2): 101, 102" in out
    for acct, suffix in ((1, ""), (2, "_a2")):
        after = ads_of(public(tmp_path, acct))
        assert set(after) == {"103" + suffix, "104" + suffix, "105" + suffix}         # 106 does not come in
        assert all(after[i] == before[acct][i] for i in after)                         # price, title, photo, Id: as published
        assert set(published(tmp_path, acct)) == {"103", "104", "105"}
        assert events(tmp_path, acct) == [("101", "removed", "low_stock"), ("102", "removed", "absent_from_supplier")]
    conn = store.connect(tmp_path / "t.db")
    retired = {(r[0], r[1]) for r in conn.execute("SELECT goods_id, ad_id FROM retired_ad WHERE account_id = 2")}
    assert retired == {("101", "101_a2"), ("102", "102_a2")}                           # the Id the ad had: the same tire takes it later
    assert store.load_suppressed(conn, 1) >= {"101"}
    conn.close()
    assert alerts(tmp_path) == []


def test_the_hourly_cycle_after_the_pass_goes_on_as_usual(tmp_path, capsys):
    prepared(tmp_path)
    run(tmp_path, FIVE)
    run(tmp_path, AFTER, "--stock-only")
    capsys.readouterr()
    assert run(tmp_path, None) == 0                               # the same unload: prices, names, photos and the new position now
    out = capsys.readouterr()
    assert "не совпадает" not in out.out + out.err and "error" not in out.out
    after = ads_of(public(tmp_path, 1))
    assert set(after) == {"103", "104", "105", "106"}
    assert set(published(tmp_path, 1)) == {"103", "104", "105", "106"}
    assert [e for e in events(tmp_path, 1)] == [("101", "removed", "low_stock"), ("102", "removed", "absent_from_supplier")]   # once


def test_nothing_to_take_off_writes_nothing(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, FIVE)
    folder = tmp_path / "feed" / token(tmp_path, 1)
    before, draft = public(tmp_path), (folder / "next" / "avito_feed.xml").read_bytes()
    stamp = (folder / "avito_feed.xml").stat().st_mtime_ns
    assert run(tmp_path, rows(*(dict(NNOMMODIF=str(n), NPRICE_DELAYED="7000") for n in range(101, 106))), "--stock-only") == 0
    assert public(tmp_path) == before and (folder / "avito_feed.xml").stat().st_mtime_ns == stamp
    assert (folder / "next" / "avito_feed.xml").read_bytes() == draft
    assert "сняты" not in capsys.readouterr().out


def test_the_pass_is_skipped_while_another_process_holds_the_feeds(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, FIVE)
    before = public(tmp_path)
    held = publishing.try_feed_lock(tmp_path / "feed")
    try:
        assert run(tmp_path, AFTER, "--stock-only") == 0
    finally:
        held.close()
    assert public(tmp_path) == before and "пропущена" in capsys.readouterr().out
    assert set(published(tmp_path)) == {"101", "102", "103", "104", "105"}


def test_the_guard_against_mass_removal_holds_and_leaves_the_alert_to_the_hourly_cycle(tmp_path, capsys):
    prepared(tmp_path, accounts=("A",), guard_min=2)
    run(tmp_path, FIVE)
    before = public(tmp_path)
    capsys.readouterr()
    code = run(tmp_path, rows(dict(NNOMMODIF="101", NREST="1"), dict(NNOMMODIF="102", NREST="0"), dict(NNOMMODIF="105")), "--stock-only")
    assert code == 0 and public(tmp_path) == before and len(published(tmp_path)) == 5
    assert all(p.first_hold is None for p in published(tmp_path).values())       # nothing written: the hourly cycle decides the hold
    out = capsys.readouterr().out
    assert "A: защита от массового снятия, снятие оставлено часовому циклу: 101 (low_stock), 102 (low_stock)" in out
    assert alerts(tmp_path) == []                                               # every 15 minutes it would be the same news again
    run(tmp_path, None)                                                         # the hourly cycle holds and tells, with the list
    assert "A, удерживается 4:" in alerts(tmp_path)[-1]


def test_the_guard_counts_what_was_taken_off_in_the_last_hour(tmp_path, capsys):
    """A catalog that melts bit by bit: each pass alone is under the threshold, together they are not."""
    prepared(tmp_path, accounts=("A",), guard_min=2)
    run(tmp_path, rows(*(dict(NNOMMODIF=str(n)) for n in range(101, 109))))
    left = [dict(NNOMMODIF=str(n)) for n in range(103, 109)]
    assert run(tmp_path, rows(*left), "--stock-only") == 0                     # 2 of 8: within the guard
    assert set(published(tmp_path)) == {str(n) for n in range(103, 109)}
    assert run(tmp_path, rows(*left[1:]), "--stock-only") == 0                  # 1 more within the hour: 3 > 2
    assert set(published(tmp_path)) == {str(n) for n in range(103, 109)}
    assert "снятие оставлено часовому циклу: 103 (absent_from_supplier)" in capsys.readouterr().out
    conn = store.connect(tmp_path / "t.db")
    conn.execute("UPDATE ad_event SET at = '2026-01-01T00:00:00'")             # an hour later: the earlier removals no longer count
    conn.commit()
    conn.close()
    run(tmp_path, rows(*left[1:]), "--stock-only")
    assert set(published(tmp_path)) == {str(n) for n in range(104, 109)}


def test_a_tire_type_left_out_of_the_account_is_the_hourly_cycle_s_change(tmp_path):
    """The owner left summer tires out of the account: a setting, applied by the hourly cycle; the pass takes off only for stock."""
    prepared(tmp_path, accounts=("A",), enabled=[["IKON TYRES", "winter"], ["IKON TYRES", "summer"]],
             pricing={"IKON TYRES|winter": {"pct_small": -3.0, "pct_large": -3.0}, "IKON TYRES|summer": {"pct_small": -3.0, "pct_large": -3.0}})
    mixed = lambda low: rows(dict(NNOMMODIF="101", NREST=low), dict(NNOMMODIF="102"), dict(NNOMMODIF="103"),
                             dict(NNOMMODIF="104", SSEASON="Летняя"), dict(NNOMMODIF="105", SSEASON="Летняя"))
    run(tmp_path, mixed("10"))
    assert set(published(tmp_path)) == {"101", "102", "103", "104", "105"}
    conn = store.connect(tmp_path / "t.db")
    connections.set_excluded_types(conn, 1, ["Летние"], NOW, "owner")
    conn.close()
    run(tmp_path, mixed("0"), "--stock-only")
    assert set(published(tmp_path)) == {"102", "103", "104", "105"} and events(tmp_path) == [("101", "removed", "low_stock")]
    run(tmp_path, None)
    assert set(published(tmp_path)) == {"102", "103"}


def test_a_critical_finding_of_the_check_keeps_the_published_feed(tmp_path, monkeypatch, capsys):
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, FIVE)
    before = public(tmp_path)
    monkeypatch.setattr(cycle, "gate", lambda *a, **k: [feed_check.Finding("price", "critical", "103", "Цена не задана")])
    capsys.readouterr()
    assert run(tmp_path, AFTER, "--stock-only") == 0
    assert public(tmp_path) == before and set(published(tmp_path)) == {"101", "102", "103", "104", "105"}
    assert "A: error, фид прежний: проверка фида не пройдена" in capsys.readouterr().out
    assert alerts(tmp_path) == []                                               # the hourly cycle's check tells it


def test_a_pass_that_breaks_by_itself_alerts_with_the_same_text_each_time(tmp_path, monkeypatch, capsys):
    from tires_sync import sync

    def broken(*a, **k):
        raise RuntimeError("boom %s" % len(alerts(tmp_path)))
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, FIVE)
    before = public(tmp_path)
    monkeypatch.setattr(sync, "plan_decisions", broken)
    assert run(tmp_path, AFTER, "--stock-only") == 0 and run(tmp_path, AFTER, "--stock-only") == 0
    assert public(tmp_path) == before
    assert alerts(tmp_path) == ["Tires Sync: проверка остатков между циклами не работает (A: RuntimeError), снимает только часовой цикл. "
                                "Подробности в журнале tires-stock."]                # said once: the same text is not repeated


def test_the_draft_of_the_pass_passes_the_real_check(tmp_path):
    """No monkeypatch: the cycle's own check finds nothing critical in the published file minus the fallen ads."""
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, FIVE)
    (tmp_path / "unload.xml").write_bytes(AFTER)
    from datetime import date
    from tires_sync.catalog import parse_reference
    from tires_sync.suppliers import load_all_parsed
    from tires_sync.settings_store import active_settings
    conn = store.connect(tmp_path / "t.db")
    parsed, _sha, _notes = load_all_parsed(conn, active_settings(conn), tmp_path / "unload.xml")
    [result] = cycle.stock_pass(conn, parsed, parse_reference(REF), date(2026, 10, 24), tmp_path / "feed")
    assert (result.ok, result.removed, result.feed_count) == (True, ("101", "102"), 3)
    assert result.error == "предупреждения проверки: shrink: 1 (например -)"     # 5 -> 3 in a tiny feed: only the warning
    conn.close()


def test_a_held_position_that_is_back_and_a_broken_row_stay_untouched(tmp_path):
    """A row that broke since the hourly cycle (stock unknown) is neither taken off nor judged by stock: the hourly cycle holds it."""
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, FIVE)
    before = ads_of(public(tmp_path))
    assert run(tmp_path, rows(dict(NNOMMODIF="101", NREST="x"), dict(NNOMMODIF="102", NREST="1"), dict(NNOMMODIF="103"),
                              dict(NNOMMODIF="104"), dict(NNOMMODIF="105")), "--stock-only") == 0
    after = ads_of(public(tmp_path))
    assert set(after) == {"101", "103", "104", "105"} and after["101"] == before["101"]


def test_a_failed_download_skips_the_pass_quietly(tmp_path, monkeypatch, capsys):
    from tires_sync import cli
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, FIVE)
    before = public(tmp_path)
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    conn = store.connect(tmp_path / "t.db")
    connections.set_config(conn, "webmim", 0, {"base_url": "https://a.example", "mode": "apikey", "login": ""}, NOW, "owner")
    connections.set_secret(conn, load_box(key_file), "webmim", 0, "api_key", "K" * 32, NOW, "owner")
    conn.close()
    (tmp_path / "unload.xml").write_bytes(AFTER)                 # an unload on disk that would take two off: not used without a fresh one
    monkeypatch.setattr(cli, "_http", lambda: (lambda method, url, headers, body: (500, {}, b"down")))
    code = main(["run-cycle", "--publish", "--stock-only", "--fetch", "--key-file", str(key_file)] + args_of(tmp_path))
    assert code == 0 and "выгрузка не скачана" in capsys.readouterr().out
    assert public(tmp_path) == before and alerts(tmp_path) == []


def test_an_account_with_publication_off_is_not_touched(tmp_path):
    prepared(tmp_path)
    run(tmp_path, FIVE)
    conn = store.connect(tmp_path / "t.db")
    connections.set_live(conn, 2, False, NOW, "owner")
    conn.close()
    before = public(tmp_path, 2)
    run(tmp_path, AFTER, "--stock-only")
    assert public(tmp_path, 2) == before and len(published(tmp_path, 2)) == 5
    assert set(published(tmp_path, 1)) == {"103", "104", "105"}


def test_an_ad_under_a_bound_id_and_its_set_of_four_leave_together(tmp_path):
    """An account moved from a mirror keeps the Ids its ads have (`adopted_<account>`); a set of four («-x4») leaves with its single ad."""
    from tires_sync import mirror_feed
    prepared(tmp_path, pack4=True)
    conn = store.connect(tmp_path / "t.db")
    mirror_feed.bind_ids(conn, 2, {"101": "old-777"}, NOW, "test")
    conn.close()
    run(tmp_path, FIVE)
    before = ads_of(public(tmp_path, 2))
    assert {"old-777", "101-x4_a2", "102_a2", "102-x4_a2"} <= set(before)
    run(tmp_path, rows(dict(NNOMMODIF="101", NREST="3"), *(dict(NNOMMODIF=str(n)) for n in range(102, 106))), "--stock-only")
    after = ads_of(public(tmp_path, 2))
    assert set(before) - set(after) == {"old-777", "101-x4_a2"} and all(after[i] == before[i] for i in after)
    conn = store.connect(tmp_path / "t.db")
    assert [tuple(r) for r in conn.execute("SELECT goods_id, ad_id FROM retired_ad WHERE account_id = 2")] == [("101", "old-777")]
    conn.close()
    first = ads_of(public(tmp_path, 1))
    assert {"101", "101-x4"}.isdisjoint(first) and {"102", "102-x4"} <= set(first)


def test_an_account_with_alias_ids_loses_the_ads_of_the_fallen_position_only(tmp_path):
    """Ids with nothing of the code in them (the alias scheme, `k…`): the code behind each Id is the cycle's own record."""
    from tires_sync import mirror_feed
    prepared(tmp_path, pack4=True)
    conn = store.connect(tmp_path / "t.db")
    mirror_feed.set_alias_scheme(conn, 2, True, NOW, "test")
    conn.close()
    run(tmp_path, FIVE)
    before = ads_of(public(tmp_path, 2))
    alias = mirror_feed.alias_id(2, "103")
    assert alias in before and alias + "-x4" in before and not any(i.startswith("103") for i in before)
    run(tmp_path, rows(dict(NNOMMODIF="103", NREST="0"), *(dict(NNOMMODIF=str(n)) for n in (101, 102, 104, 105))), "--stock-only")
    after = ads_of(public(tmp_path, 2))
    assert set(before) - set(after) == {alias, alias + "-x4"} and all(after[i] == before[i] for i in after)
    assert set(published(tmp_path, 2)) == {"101", "102", "104", "105"}


def test_the_timer_and_the_service_of_the_pass():
    import re
    from pathlib import Path
    deploy = Path(__file__).resolve().parent.parent / "deploy"
    timer = (deploy / "tires-stock.timer").read_text(encoding="utf-8")
    keys = re.search(r"^TIMER_KEYS='(.*)'$", (deploy / "tires-deploy.sh").read_text(encoding="utf-8"), re.M).group(1)
    assert timer.isascii() and all(re.match(keys, line) for line in timer.split("\n"))     # tires-deploy keeps it in sync once installed
    assert re.findall(r"^OnCalendar=(.*)$", timer, re.M) == ["*-*-* *:10,25,55:00"]          # never at :40, the hourly cycle's minute
    service = (deploy / "tires-stock.service").read_text(encoding="utf-8")
    hourly = (deploy / "tires-cycle.service").read_text(encoding="utf-8")
    run_of = lambda text: re.search(r"^ExecStart=(.*)$", text, re.M).group(1)
    assert run_of(service) == run_of(hourly).replace("run-cycle --fetch --publish", "run-cycle --stock-only --fetch --publish")
    strip = lambda text: [l for l in text.split("\n") if not l.startswith(("Description=", "ExecStart="))]
    assert strip(service) == strip(hourly)                                                     # the same user, rights and paths


def test_a_removal_for_another_reason_than_stock_waits_for_the_hourly_cycle(tmp_path):
    """The owner switched 105 off by hand: that is a change of settings, the hourly cycle applies it; the light pass takes off only for stock."""
    from tires_sync.settings_store import confirm_draft, create_draft
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, FIVE)
    conn = store.connect(tmp_path / "t.db")
    confirm_draft(conn, create_draft(conn, {"overrides": {"1:105": "off"}}, NOW), NOW, "owner", strong=True)
    conn.close()
    run(tmp_path, rows(dict(NNOMMODIF="101", NREST="0"), *(dict(NNOMMODIF=str(n)) for n in range(102, 106))), "--stock-only")
    assert set(ads_of(public(tmp_path))) == {"102", "103", "104", "105"}
    run(tmp_path, None)
    assert set(ads_of(public(tmp_path))) == {"102", "103", "104"}


def test_the_guard_alert_lists_at_most_twenty_positions_for_all_accounts_together(tmp_path):
    from tires_sync import cli
    from tires_sync.cycle import AccountResult
    prepared(tmp_path, accounts=("A",))
    conn = store.connect(tmp_path / "t.db")
    many = tuple(("%d" % n, "absent_from_supplier") for n in range(200, 215))
    text = cli._guard_alert_text(conn, [AccountResult(1, "A", True, guarded=many), AccountResult(2, "B", True, guarded=many)], None)
    conn.close()
    assert text.count(" — нет в выгрузке") == 20 and "A, удерживается 15:" in text and "B, удерживается 15:" in text
    assert "  … и ещё 10" in text and len(text) < 4000


EIGHT = [dict(NNOMMODIF=str(n)) for n in range(101, 109)]


def test_a_catalog_that_melts_between_passes_and_the_hourly_cycle_trips_the_hourly_guard(tmp_path):
    """Owner's team lead, 2026-10-24: within any hour at most one threshold of removals, the light passes included."""
    prepared(tmp_path, accounts=("A",), guard_min=2)
    run(tmp_path, rows(*EIGHT))
    run(tmp_path, rows(*EIGHT[2:]), "--stock-only")                            # the pass takes 2 (within the guard)
    assert set(published(tmp_path)) == {str(n) for n in range(103, 109)}
    assert cycle.pass_removals_lately(store.connect(tmp_path / "t.db"), 1) == 2
    assert run(tmp_path, rows(*EIGHT[3:])) == 3                                 # the hourly cycle: 2 earlier + 1 now > 2
    now = published(tmp_path)
    assert set(now) == {str(n) for n in range(103, 109)} and now["103"].first_hold is not None
    assert "A, удерживается 1:\n  (за последний час проверка остатков уже сняла 2, защита считает их вместе)" in alerts(tmp_path)[-1]


def test_without_passes_the_hourly_cycle_decides_as_before(tmp_path):
    prepared(tmp_path, accounts=("A",), guard_min=2)
    run(tmp_path, rows(*EIGHT))
    assert run(tmp_path, rows(*EIGHT[2:])) == 0 and run(tmp_path, rows(*EIGHT[3:])) == 0    # its own earlier removals are not counted
    assert set(published(tmp_path)) == {str(n) for n in range(104, 109)}
    assert cycle.pass_removals_lately(store.connect(tmp_path / "t.db"), 1) == 0


def test_the_removals_of_passes_older_than_an_hour_no_longer_count(tmp_path):
    prepared(tmp_path, accounts=("A",), guard_min=2)
    run(tmp_path, rows(*EIGHT))
    run(tmp_path, rows(*EIGHT[2:]), "--stock-only")
    conn = store.connect(tmp_path / "t.db")
    store.set_state(conn, "stock_pass_removed_1", json.dumps([["2026-01-01T00:00:00", 2]]))
    assert cycle.pass_removals_lately(conn, 1) == 0
    cycle.remember_pass_removals(conn, 1, 1)                                     # an old mark is dropped when a new one comes
    assert len(json.loads(store.get_state(conn, "stock_pass_removed_1"))) == 1
    store.set_state(conn, "stock_pass_removed_1", json.dumps([["2026-01-01T00:00:00", 2]]))
    conn.close()
    assert run(tmp_path, rows(*EIGHT[3:])) == 0
    assert set(published(tmp_path)) == {str(n) for n in range(104, 109)}


def two_lots(stock_a, stock_b):
    same = dict(SLOAD="94", SWIDTH="205", TERRITORY_RN="T1")                  # the same tire in two lots (two supplier codes), test_same_tire.py
    return catalog_xml(commodity_xml(NNOMMODIF="106", NREST=str(stock_a), SMNFCODE="LOT-A", **same),
                       commodity_xml(NNOMMODIF="125", NREST=str(stock_b), SMNFCODE="LOT-B", **same),
                       *(commodity_xml(NNOMMODIF=str(n), TERRITORY_RN="T1") for n in range(101, 104)))     # other tires of the feed


def test_a_lot_whose_id_the_next_lot_of_the_tire_takes_is_left_to_the_hourly_cycle(tmp_path):
    """feed-reviewer, 2026-10-24: lot 106 falls to 0 while lot 125 of the same tire has stock. The hourly cycle takes 106 off and publishes
    125 under the Id 106 in one step; the pass taking 106 off alone would make 125 come out under a new Id (Avito: «Повторное размещение»)."""
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, two_lots(8, 4))
    assert set(ads_of(public(tmp_path))) == {"101", "102", "103", "106"}
    conn = store.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, seen_at, first_seen) VALUES (1, 1, '106', ?, '2026-07-16T00:00:00')", (NOW,))
    conn.commit()
    conn.close()
    run(tmp_path, two_lots(0, 50), "--stock-only")
    assert "106" in ads_of(public(tmp_path)) and "106" in published(tmp_path)          # not taken off by the pass
    run(tmp_path, None)
    assert set(ads_of(public(tmp_path))) == {"101", "102", "103", "106"}               # the next lot, under the Id the cabinet knows
    assert set(published(tmp_path)) == {"101", "102", "103", "125"}


def test_a_renewed_lot_whose_id_the_next_lot_takes_is_left_to_the_hourly_cycle_too(tmp_path):
    """tails2 Ф3: the same with the Id of 106 renewed (`106_r20261020`): the pass compares the Id of the file (with the mark) with the Id the
    next lot takes, so that Id must carry the mark too; the hourly cycle then hands the renewed Id over."""
    from tires_sync import mirror_feed
    prepared(tmp_path, accounts=("A",))
    conn = store.connect(tmp_path / "t.db")
    mirror_feed.reissue_own(conn, 1, ["106"], "20261020", NOW, "o")
    conn.close()
    run(tmp_path, two_lots(8, 4))
    assert set(ads_of(public(tmp_path))) == {"101", "102", "103", "106_r20261020"}
    conn = store.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, seen_at, first_seen) VALUES (1, 1, '106_r20261020', ?, '2026-07-16T00:00:00')",
                 (NOW,))
    conn.commit()
    conn.close()
    run(tmp_path, two_lots(0, 50), "--stock-only")
    assert "106_r20261020" in ads_of(public(tmp_path)) and "106" in published(tmp_path)          # not taken off by the pass
    run(tmp_path, None)
    assert set(ads_of(public(tmp_path))) == {"101", "102", "103", "106_r20261020"}
    assert set(published(tmp_path)) == {"101", "102", "103", "125"}


SILENT = "Tires Sync: проверка остатков между циклами больше часа не может опубликовать фид (A): снимает только часовой цикл. " \
         "Причина в журнале tires-stock."


def test_a_pass_that_cannot_publish_for_over_an_hour_in_a_row_alerts_once(tmp_path, monkeypatch, capsys):
    """tails2 Ф6: a draft of the pass that fails its check is told only in the journal (the hourly cycle's check says the same); failing for
    an hour in a row (three passes and the fourth), the owner is told once."""
    from tires_sync import cli
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, FIVE)
    clock = [1_791_900_000.0]
    monkeypatch.setattr(cli, "_epoch", lambda: clock[0])
    monkeypatch.setattr(cycle, "gate", lambda *a, **k: [feed_check.Finding("price", "critical", "103", "Цена не задана")])
    for minutes in (0, 15, 45):                                                  # :10, :25, :55
        clock[0] = 1_791_900_000.0 + minutes * 60
        assert run(tmp_path, AFTER, "--stock-only") == 0
    assert alerts(tmp_path) == []
    clock[0] = 1_791_900_000.0 + 60 * 60                                         # the next :10
    assert run(tmp_path, AFTER, "--stock-only") == 0
    assert alerts(tmp_path) == [SILENT]
    clock[0] += 15 * 60
    assert run(tmp_path, AFTER, "--stock-only") == 0
    assert alerts(tmp_path) == [SILENT]                                          # not again


def test_a_pass_that_goes_through_or_has_nothing_to_take_off_starts_the_hour_again(tmp_path, monkeypatch):
    from tires_sync import cli
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, FIVE)
    clock = [1_791_900_000.0]
    monkeypatch.setattr(cli, "_epoch", lambda: clock[0])
    monkeypatch.setattr(cycle, "gate", lambda *a, **k: [feed_check.Finding("price", "critical", "103", "Цена не задана")])
    run(tmp_path, AFTER, "--stock-only")
    clock[0] += 30 * 60
    assert run(tmp_path, FIVE, "--stock-only") == 0                              # nothing to take off: the count starts again
    clock[0] += 35 * 60
    run(tmp_path, AFTER, "--stock-only")
    clock[0] += 30 * 60
    run(tmp_path, AFTER, "--stock-only")
    assert alerts(tmp_path) == []                                                # failing for 30 minutes only
    clock[0] += 30 * 60
    run(tmp_path, AFTER, "--stock-only")
    assert alerts(tmp_path) == [SILENT]


def test_failed_passes_with_a_gap_of_skipped_ones_between_are_not_in_a_row(tmp_path, monkeypatch):
    """Review of Ф6: a failure, then two hours of skipped passes (the feeds busy, no unload), then a failure: two failures, not two hours."""
    from tires_sync import cli
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, FIVE)
    clock = [1_791_900_000.0]
    monkeypatch.setattr(cli, "_epoch", lambda: clock[0])
    monkeypatch.setattr(cycle, "gate", lambda *a, **k: [feed_check.Finding("price", "critical", "103", "Цена не задана")])
    run(tmp_path, AFTER, "--stock-only")
    clock[0] += 2 * 60 * 60
    run(tmp_path, AFTER, "--stock-only")
    assert alerts(tmp_path) == []


def test_one_skipped_pass_between_failed_ones_keeps_the_count(tmp_path, monkeypatch):
    """Review 2 of Ф6 (D): :25 fails, :55 is skipped (the hourly cycle holds the feeds), :10 and :25 fail: an hour in a row."""
    from tires_sync import cli
    prepared(tmp_path, accounts=("A",))
    run(tmp_path, FIVE)
    clock = [1_791_900_000.0]
    monkeypatch.setattr(cli, "_epoch", lambda: clock[0])
    monkeypatch.setattr(cycle, "gate", lambda *a, **k: [feed_check.Finding("price", "critical", "103", "Цена не задана")])
    for minutes in (0, 45, 60):
        clock[0] = 1_791_900_000.0 + minutes * 60
        run(tmp_path, AFTER, "--stock-only")
    assert alerts(tmp_path) == [SILENT]
