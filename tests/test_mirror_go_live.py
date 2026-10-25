"""`mirror-go-live`: bring a mirrored account to Avito in one checked step (rebuild, verify, publish, attach the link, start the upload)."""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from http_fakes import ScriptedHttp
from test_cli_avito import EMPTY_PROFILE, NOW, TOKEN, env  # noqa: F401  (the fixture of the Avito cli tests)
from tires_sync import cli, connections, mirror_feed, store
from tires_sync.cli import main


def unload_for(tmp, present=("a",), low=()):
    from helpers import catalog_xml, commodity_xml
    items = [commodity_xml(NNOMMODIF=g, SMNFCODE="C" + g, NREST="2" if g in low else "12") for g in present]
    path = tmp / "unload.xml"
    path.write_bytes(catalog_xml(*items))
    return ["--unload", str(path)]


def ads(*ids):
    body = "".join("<Ad><Id>%s</Id><Title>t</Title><Price>100</Price><DateEnd>2026-11-11</DateEnd></Ad>" % i for i in ids)
    return ('<?xml version="1.0" encoding="utf-8"?><Ads formatVersion="3" target="Avito.ru">%s</Ads>' % body).encode()


def go_live(publish, tmp=None, **kw):
    return ["mirror-go-live" if a == "avito-publish" else a for a in publish] + (unload_for(tmp, **kw) if tmp else [])


def prepare(tmp, ids=("a_1", "b_1", "c_1")):
    conn = store.connect(tmp / "t.db")
    mirror_feed.import_feed(conn, 1, ads(*ids), NOW)
    conn.close()


def test_without_confirm_it_builds_and_checks_but_changes_nothing(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    prepare(tmp)
    http = ScriptedHttp(TOKEN, EMPTY_PROFILE)
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(go_live(publish, tmp, present=("a", "b", "c"))) == 0
    out = capsys.readouterr().out
    assert "Ничего не изменено" in out and "3" in out
    assert not (tmp / "feed" / token / "avito_feed.xml").exists() and connections.get_account(store.connect(tmp / "t.db"), 1)["live"] == 0
    assert all(c[0] == "GET" or "token" in c[1] for c in http.calls)                                    # nothing was written at Avito


def test_with_confirm_it_publishes_attaches_the_link_and_starts_the_upload_once(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    prepare(tmp)
    http = ScriptedHttp(TOKEN, EMPTY_PROFILE, (200, {}), (200, {}))
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(cli, "_epoch", lambda: 50_000.0)
    assert main(go_live(publish, tmp, present=("a", "b", "c")) + ["--confirm"]) == 0
    conn = store.connect(tmp / "t.db")
    assert connections.get_account(conn, 1)["live"] == 1
    public = ET.fromstring((tmp / "feed" / token / "avito_feed.xml").read_bytes())
    assert [a.findtext("Id") for a in public] == ["a_1", "b_1", "c_1"]                                  # the same Ids, nothing lost
    assert [(c[0], c[1].rsplit("/", 2)[-2:]) for c in http.calls[2:]] == [("POST", ["v2", "profile"]), ("POST", ["v1", "upload"])]
    assert "загрузка запущена" in capsys.readouterr().out


def test_a_feed_that_misses_an_ad_is_never_published(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    prepare(tmp)
    http = ScriptedHttp(TOKEN, EMPTY_PROFILE)
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(mirror_feed, "missing_ids", lambda conn, account, data, skip=(): ["b_1"])
    assert main(go_live(publish, tmp, present=("a", "b", "c")) + ["--confirm"]) != 0
    assert "не хватает" in capsys.readouterr().err
    assert not (tmp / "feed" / token / "avito_feed.xml").exists() and connections.get_account(store.connect(tmp / "t.db"), 1)["live"] == 0
    assert all(c[0] == "GET" or "token" in c[1] for c in http.calls)


def test_an_account_that_is_not_a_mirror_or_has_no_ads_is_refused(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    http = ScriptedHttp(TOKEN, EMPTY_PROFILE)
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(go_live(publish, tmp) + ["--confirm"]) != 0
    assert "зеркало" in capsys.readouterr().err and connections.get_account(store.connect(tmp / "t.db"), 1)["live"] == 0


def test_what_the_supplier_does_not_have_is_not_published_and_the_report_says_so(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    prepare(tmp, ids=("a_1", "b_1", "c_1", "d_1"))
    http = ScriptedHttp(TOKEN, EMPTY_PROFILE, (200, {}), (200, {}))
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(cli, "_epoch", lambda: 50_000.0)
    assert main(go_live(publish, tmp, present=("a", "b", "c"), low=("c",)) + ["--confirm"]) == 0                  # d is absent, c is below the threshold
    public = ET.fromstring((tmp / "feed" / token / "avito_feed.xml").read_bytes())
    assert [a.findtext("Id") for a in public] == ["a_1", "b_1"]
    out = capsys.readouterr().out
    assert "нет у поставщика: 1" in out and "остаток ниже порога: 1" in out


def test_the_command_refuses_to_publish_when_the_unload_is_not_readable(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    prepare(tmp)
    http = ScriptedHttp(TOKEN, EMPTY_PROFILE)
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(go_live(publish) + ["--unload", str(tmp / "nowhere.xml"), "--confirm"]) != 0
    assert not (tmp / "feed" / token / "avito_feed.xml").exists() and connections.get_account(store.connect(tmp / "t.db"), 1)["live"] == 0


def test_a_stale_pending_state_of_an_earlier_cycle_does_not_stop_the_mirror_from_going_live(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    prepare(tmp)
    conn = store.connect(tmp / "t.db")
    store.save_pending(conn, 1, b"<Ads>an older cycle</Ads>", [])                                      # left by a cycle of the account before it was a mirror
    conn.close()
    http = ScriptedHttp(TOKEN, EMPTY_PROFILE, (200, {}), (200, {}))
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(cli, "_epoch", lambda: 50_000.0)
    assert main(go_live(publish, tmp, present=("a", "b", "c")) + ["--confirm"]) == 0
    assert "загрузка запущена" in capsys.readouterr().out


def test_the_timer_publish_of_a_mirror_ignores_a_stale_pending_state(env):
    from tires_sync import publishing
    tmp, token, base, publish = env
    prepare(tmp)
    conn = store.connect(tmp / "t.db")
    store.save_pending(conn, 1, b"<Ads>an older cycle</Ads>", [])
    connections.set_live(conn, 1, True, "2026-10-14T06:00:00", "test")
    conn.commit()
    account = connections.get_account(conn, 1)
    mirror_feed.write_candidate(conn, account, tmp / "feed", __import__("datetime").date(2026, 10, 14), cli.active_settings(conn))
    result = publishing.publish_live_accounts(conn, tmp / "feed")
    assert result[0]["ok"], result


def test_a_failed_publication_leaves_the_account_switched_off(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    prepare(tmp)
    conn = store.connect(tmp / "t.db")
    store.set_state(conn, "avito_last_upload_1", repr(49_000.0))                                # an upload an hour ago minus a moment: the limit refuses
    conn.close()
    http = ScriptedHttp(TOKEN, EMPTY_PROFILE)
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(cli, "_epoch", lambda: 50_000.0)
    assert main(go_live(publish, tmp, present=("a", "b", "c")) + ["--confirm"]) != 0
    assert connections.get_account(store.connect(tmp / "t.db"), 1)["live"] == 0                 # not left on for the timer to publish
