from __future__ import annotations

from datetime import date

import pytest
from cryptography.fernet import Fernet

from helpers import catalog_xml, commodity_xml, settings_dict
from http_fakes import ScriptedHttp
from tires_sync import cli, connections, cycle, store
from tires_sync.catalog import parse_reference
from tires_sync.cli import main
from tires_sync.secretbox import create_key_file, load_box
from tires_sync.settings_store import init_settings

NOW = "2026-10-10T12:00:00"
REF = '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>'
TODAY = date(2026, 10, 10)


def prepared(tmp_path, **settings):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(**settings), NOW)
    return conn


def test_each_account_gets_its_own_feed_in_its_own_private_folder(tmp_path):
    conn = prepared(tmp_path)
    connections.create_account(conn, "Основной", NOW, "owner")
    connections.create_account(conn, "Второй", NOW, "owner")
    results = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, tmp_path / "feed")
    assert [(r.name, r.ok) for r in results] == [("Основной", True), ("Второй", True)]
    tokens = [a["feed_token"] for a in connections.list_accounts(conn)]
    for token in tokens:
        assert (tmp_path / "feed" / token / "next" / "avito_feed.xml").exists()
    assert results[0].feed_count == 1 and results[0].counts


def test_a_failing_account_does_not_stop_the_others(tmp_path, monkeypatch):
    conn = prepared(tmp_path)
    connections.create_account(conn, "Первый", NOW, "owner")
    connections.create_account(conn, "Второй", NOW, "owner")
    real = cycle.run_cycle
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(cycle, "run_cycle", flaky)
    results = cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, tmp_path / "feed")
    assert (results[0].ok, results[1].ok) == (False, True)
    assert "boom" in results[0].error


def test_an_empty_feed_is_reported_for_the_account_not_raised(tmp_path):
    conn = prepared(tmp_path)
    connections.create_account(conn, "Основной", NOW, "owner")
    unmatched = catalog_xml(commodity_xml(SMODEL="Unknown Model"))
    [result] = cycle.run_for_accounts(conn, unmatched, parse_reference(REF), TODAY, tmp_path / "feed")
    assert result.ok is False and "feed is empty" in result.error


def test_no_accounts_is_an_error_with_a_hint(tmp_path):
    conn = prepared(tmp_path)
    with pytest.raises(ValueError) as info:
        cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, tmp_path / "feed")
    assert "аккаунт" in str(info.value)


def files(tmp_path, xml=None):
    (tmp_path / "unload.xml").write_bytes(xml or catalog_xml(commodity_xml()))
    (tmp_path / "reference.txt").write_text(REF, encoding="utf-8")
    return ["--db", str(tmp_path / "t.db"), "--unload", str(tmp_path / "unload.xml"),
            "--reference", str(tmp_path / "reference.txt"), "--feed-root", str(tmp_path / "feed")]


def test_run_cycle_command_prints_a_summary_per_account(tmp_path, capsys):
    conn = prepared(tmp_path)
    connections.create_account(conn, "Основной", NOW, "owner")
    conn.close()
    code = main(["run-cycle"] + files(tmp_path))
    out = capsys.readouterr().out
    assert code == 0 and "Основной: ok" in out
    token = connections.list_accounts(store.connect(tmp_path / "t.db"))[0]["feed_token"]
    assert (tmp_path / "feed" / token / "next" / "avito_feed.xml").exists()


def test_run_cycle_command_exit_code_3_when_an_account_fails(tmp_path, capsys):
    conn = prepared(tmp_path)
    connections.create_account(conn, "Основной", NOW, "owner")
    conn.close()
    code = main(["run-cycle"] + files(tmp_path, catalog_xml(commodity_xml(SMODEL="Unknown Model"))))
    assert code == 3 and "Основной: error" in capsys.readouterr().out


def test_run_cycle_command_without_accounts_exits_2(tmp_path, capsys):
    prepared(tmp_path).close()
    assert main(["run-cycle"] + files(tmp_path)) == 2
    assert "аккаунт" in capsys.readouterr().err


def fetch_setup(tmp_path):
    conn = prepared(tmp_path, territory_rn="103261489424")
    connections.create_account(conn, "Основной", NOW, "owner")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_config(conn, "webmim", 0, {"base_url": "https://mimtest.svrauto.ru", "mode": "login", "login": "u"}, NOW, "owner")
    connections.set_secret(conn, box, "webmim", 0, "password", "pw", NOW, "owner")
    conn.close()
    return key_file


def test_run_cycle_with_fetch_downloads_first_and_then_runs(tmp_path, monkeypatch, capsys):
    key_file = fetch_setup(tmp_path)
    xml = catalog_xml(commodity_xml(TERRITORY_RN="103261489424"))
    script = ScriptedHttp((200, {"result": True, "authKey": "A"}), (200, '"%s"' % ("D" * 32)),
                          (200, {"result": True}), (200, xml))
    monkeypatch.setattr(cli, "_http", lambda: script)
    args = files(tmp_path, b"<CATALOG><COMMODITIES></COMMODITIES></CATALOG>") + ["--fetch", "--key-file", str(key_file)]
    code = main(["run-cycle"] + args)
    assert code == 0 and "Основной: ok" in capsys.readouterr().out
    assert (tmp_path / "unload.xml").read_bytes() == xml


def test_run_cycle_with_failed_fetch_does_not_run_on_stale_data(tmp_path, monkeypatch, capsys):
    key_file = fetch_setup(tmp_path)
    monkeypatch.setattr(cli, "_http", lambda: ScriptedHttp((401, "no")))
    args = files(tmp_path) + ["--fetch", "--key-file", str(key_file)]
    code = main(["run-cycle"] + args)
    assert code == 2 and not (tmp_path / "feed").exists()
    assert "error:" in capsys.readouterr().err


def test_each_account_uses_its_own_description_template(tmp_path):
    conn = prepared(tmp_path)
    first = connections.create_account(conn, "Основной", NOW, "owner")
    connections.create_account(conn, "Второй", NOW, "owner")
    connections.set_description_template(conn, first, "Артикул {code} / цена {price}", NOW, "owner")
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, tmp_path / "feed")
    accounts = connections.list_accounts(conn)
    custom = (tmp_path / "feed" / accounts[0]["feed_token"] / "next" / "avito_feed.xml").read_text(encoding="utf-8")
    default = (tmp_path / "feed" / accounts[1]["feed_token"] / "next" / "avito_feed.xml").read_text(encoding="utf-8")
    assert "Артикул TS1 / цена" in custom and "Для поиска:" not in custom
    assert "Для поиска:" in default


def test_several_accounts_do_not_hide_new_positions_or_split_the_baseline(tmp_path):
    conn = prepared(tmp_path)
    connections.create_account(conn, "Основной", NOW, "owner")
    connections.create_account(conn, "Второй", NOW, "owner")
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml()), parse_reference(REF), TODAY, tmp_path / "feed")
    assert [r[0] for r in conn.execute("SELECT account_id FROM run ORDER BY id")] == [1, 2]
    last = store.last_run(conn)
    assert last["account_id"] == 1
    report = store.new_positions(conn, last["id"])
    assert report["baseline"] is True and len(report["goods"]) == 1
    assert conn.execute("SELECT COUNT(*) FROM seen_goods").fetchone()[0] == 1


def test_publish_flag_publishes_only_live_accounts_and_records_their_state(tmp_path, capsys):
    conn = prepared(tmp_path)
    connections.create_account(conn, "Живой", NOW, "owner")
    connections.create_account(conn, "Спящий", NOW, "owner")
    connections.set_live(conn, 1, True, NOW, "owner")
    conn.close()
    assert main(["run-cycle", "--publish"] + files(tmp_path)) == 0
    conn = store.connect(tmp_path / "t.db")
    live, idle = connections.list_accounts(conn)
    assert (tmp_path / "feed" / live["feed_token"] / "avito_feed.xml").exists()
    assert not (tmp_path / "feed" / idle["feed_token"] / "avito_feed.xml").exists()
    assert set(store.load_published(conn, account_id=live["id"])) == {"100"}
    assert store.load_published(conn, account_id=idle["id"]) == {}
    assert "Живой: опубликован" in capsys.readouterr().out


def test_without_publish_flag_nothing_goes_public(tmp_path):
    conn = prepared(tmp_path)
    connections.create_account(conn, "Живой", NOW, "owner")
    connections.set_live(conn, 1, True, NOW, "owner")
    conn.close()
    assert main(["run-cycle"] + files(tmp_path)) == 0
    token = connections.list_accounts(store.connect(tmp_path / "t.db"))[0]["feed_token"]
    assert not (tmp_path / "feed" / token / "avito_feed.xml").exists()


def test_publish_flag_does_not_publish_when_the_cycle_itself_refused_a_shrink(tmp_path, capsys, monkeypatch):
    from datetime import datetime
    conn = prepared(tmp_path)
    connections.create_account(conn, "Живой", NOW, "owner")
    connections.set_live(conn, 1, True, NOW, "owner")
    conn.close()
    monkeypatch.setattr("tires_sync.cli.moscow_now", lambda: datetime(2026, 10, 7, 12, 0))
    many = catalog_xml(*[commodity_xml(NNOMMODIF=str(i)) for i in range(100, 110)])
    assert main(["run-cycle", "--publish"] + files(tmp_path, many)) == 0
    few = catalog_xml(commodity_xml(NNOMMODIF="100"))
    # nine positions left the unload: they are taken off at once (owner's decision of 2026-10-14), so the shrink is real and is refused
    code = main(["run-cycle", "--publish"] + files(tmp_path, few))
    out = capsys.readouterr().out
    assert code == 3 and "shrank" in out and "опубликован, объявлений: 1 " not in out
