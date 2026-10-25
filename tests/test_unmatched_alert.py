from __future__ import annotations

import json

from helpers import catalog_xml, commodity_xml, settings_dict
from http_fakes import ScriptedHttp
from test_review4_fixes import BOT, OK, env
from tires_sync import cli

REF = '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>'


def run(tmp_path, key_file, rows, monkeypatch, http):
    (tmp_path / "unload.xml").write_bytes(catalog_xml(*rows))
    (tmp_path / "reference.txt").write_text(REF, encoding="utf-8")
    monkeypatch.setattr(cli, "_http", lambda: http)
    return cli.main(["run-cycle", "--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
                     "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed"), "--key-file", str(key_file)])


def texts(http):
    return [json.loads(c[3])["text"] for c in http.calls]


def test_new_models_without_a_pair_are_announced_once(tmp_path, monkeypatch):
    conn, key_file, box = env(tmp_path, territory_rn="T1")
    conn.close()
    good = commodity_xml(NNOMMODIF="1", SMNFCODE="C1")
    odd = commodity_xml(NNOMMODIF="2", SMODEL="Weird 9000", SMNFCODE="C2")
    http = ScriptedHttp(OK, OK, OK)
    run(tmp_path, key_file, [good, odd], monkeypatch, http)
    assert len(http.calls) == 1 and "новые модели не сопоставлены (1)" in texts(http)[0] and "Ikon Tyres / Weird 9000" in texts(http)[0]
    run(tmp_path, key_file, [good, odd], monkeypatch, http)                  # the same model again: nothing new to say
    assert len(http.calls) == 1
    odd2 = commodity_xml(NNOMMODIF="3", SMODEL="Another Odd", SMNFCODE="C3")
    run(tmp_path, key_file, [good, odd, odd2], monkeypatch, http)
    assert len(http.calls) == 2 and "Another Odd" in texts(http)[1] and "Weird 9000" not in texts(http)[1]


def test_no_message_when_everything_is_paired(tmp_path, monkeypatch):
    conn, key_file, box = env(tmp_path, territory_rn="T1")
    conn.close()
    http = ScriptedHttp(OK)
    run(tmp_path, key_file, [commodity_xml(NNOMMODIF="1", SMNFCODE="C1")], monkeypatch, http)
    assert http.calls == []


def test_a_failed_cycle_says_nothing_and_does_not_forget_what_was_announced(tmp_path, monkeypatch):
    from tires_sync.cycle import AccountResult
    conn, key_file, box = env(tmp_path, territory_rn="T1")
    http = ScriptedHttp(OK, OK, OK)
    monkeypatch.setattr(cli, "_http", lambda: http)
    one = AccountResult(1, "A", True, unmatched=("Ikon Tyres / Odd",))
    cli._alert_new_unmatched(conn, box, [one])
    cli._alert_new_unmatched(conn, box, [AccountResult(1, "A", False, "boom")])      # every account failed
    cli._alert_new_unmatched(conn, box, [one])
    assert len(http.calls) == 1


def test_a_message_that_telegram_refused_is_tried_again_next_time(tmp_path, monkeypatch):
    from tires_sync.cycle import AccountResult
    conn, key_file, box = env(tmp_path, territory_rn="T1")
    http = ScriptedHttp((500, {}), OK)
    monkeypatch.setattr(cli, "_http", lambda: http)
    res = [AccountResult(1, "A", True, unmatched=("Ikon Tyres / Odd",))]
    cli._alert_new_unmatched(conn, box, res)
    store_state = __import__("tires_sync.store", fromlist=["store"]).get_state(conn, "alerted_unmatched")
    assert not store_state                                                          # not remembered: nothing was delivered
    conn.execute("DELETE FROM app_state WHERE key IN ('last_alert_at', 'last_alert_text')")
    cli._alert_new_unmatched(conn, box, res)
    assert len(http.calls) == 2


def test_the_list_is_cut_to_ten_and_models_of_every_account_count(tmp_path, monkeypatch):
    from tires_sync.cycle import AccountResult
    conn, key_file, box = env(tmp_path, territory_rn="T1")
    http = ScriptedHttp(OK)
    monkeypatch.setattr(cli, "_http", lambda: http)
    a = AccountResult(1, "A", True, unmatched=tuple("Brand / M%02d" % i for i in range(8)))
    b = AccountResult(2, "B", True, unmatched=tuple("Brand / N%02d" % i for i in range(8)))
    cli._alert_new_unmatched(conn, box, [a, b])
    text = json.loads(http.calls[0][3])["text"]
    assert "(16)" in text and "и ещё 6" in text and "N00" in text


def test_the_message_is_in_the_journal_as_a_notice_not_as_an_error(tmp_path):
    from tires_sync import store
    from tires_sync.cycle import AccountResult
    conn = store.connect(tmp_path / "t.db")
    cli._alert_new_unmatched(conn, None, [AccountResult(1, "A", True, unmatched=("Ikon Tyres / Odd",))])
    entries = store.list_audit(conn, 10)
    assert [e["action"] for e in entries] == ["notice"]
    assert store.list_audit(conn, 10, kind="errors") == [] and len(store.list_audit(conn, 10, kind="system")) == 1


def test_a_cycle_records_the_days_unit_economics_snapshot(tmp_path, monkeypatch):
    from tires_sync import store
    conn, key_file, box = env(tmp_path, territory_rn="T1")
    conn.close()
    http = ScriptedHttp(OK, OK)
    run(tmp_path, key_file, [commodity_xml(NNOMMODIF="1", SMNFCODE="C1", NREST="10")], monkeypatch, http)
    rows = store.connect(tmp_path / "t.db").execute("SELECT brand_key, ads, stock FROM economics_snapshot").fetchall()
    assert rows == [("IKON TYRES", 1, 10)]


def test_a_failed_cycle_leaves_no_dip_in_the_economics_dynamics(tmp_path, monkeypatch):
    from tires_sync import store
    conn, key_file, box = env(tmp_path, territory_rn="T1")
    conn.close()
    http = ScriptedHttp(OK, OK, OK)
    # the only position is a model without a pair: the feed would be empty and the account's cycle fails
    code = run(tmp_path, key_file, [commodity_xml(NNOMMODIF="2", SMODEL="Weird 9000", SMNFCODE="C2")], monkeypatch, http)
    assert code != 0
    assert store.connect(tmp_path / "t.db").execute("SELECT COUNT(*) FROM economics_snapshot").fetchone()[0] == 0


def test_the_same_alert_is_told_again_only_after_six_hours_and_another_one_at_once(tmp_path, monkeypatch):
    """The cycle runs every hour since 2026-10-20: a lasting failure is told as often as before (every 6 hours), not every cycle."""
    conn, key_file, box = env(tmp_path, territory_rn="T1")
    http = ScriptedHttp(OK, OK, OK, OK)
    monkeypatch.setattr(cli, "_http", lambda: http)
    clock = [1_000_000.0]
    monkeypatch.setattr(cli, "_epoch", lambda: clock[0])
    cli._alert(conn, box, "сбой выгрузки")
    clock[0] += 5 * 3600                                                                 # the next cycles: not told again
    cli._alert(conn, box, "сбой выгрузки")
    assert len(http.calls) == 1
    clock[0] += 3600 + 1                                                                 # six hours after the last message: told again
    cli._alert(conn, box, "сбой выгрузки")
    assert len(http.calls) == 2
    clock[0] += 60
    cli._alert(conn, box, "другая беда")                                                 # another text goes out at once
    assert len(http.calls) == 3
    clock[0] += 60
    assert cli._alert(conn, box, "служба упала", service=True) is True                   # the service's own alert right after the cycle said it
    assert len(http.calls) == 3
