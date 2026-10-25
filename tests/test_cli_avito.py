from __future__ import annotations

import json

import pytest

from helpers import settings_dict
from http_fakes import ScriptedHttp
from tires_sync import cli, connections, store
from tires_sync.cli import main
from tires_sync.secretbox import create_key_file, load_box
from tires_sync.settings_store import init_settings

NOW = "2026-10-10T12:00:00"
TOKEN = (200, {"access_token": "t", "expires_in": 3600})
EMPTY_PROFILE = (200, {"autoload_enabled": True, "feeds_data": [], "report_email": "a@b.c", "schedule": []})


@pytest.fixture
def env(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW)
    account = connections.create_account(conn, "Основной", NOW, "owner")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_secret(conn, box, "avito", account, "client_id", "CLIENTID-12345678", NOW, "owner")
    connections.set_secret(conn, box, "avito", account, "client_secret", "SECRETVALUE-XYZ", NOW, "owner")
    connections.set_avito_identity(conn, account, 80000001, "Основной", "79000000000")             # as «Проверить подключение» records it
    token = connections.get_account(conn, account)["feed_token"]
    conn.close()
    feed_dir = tmp_path / "feed" / token
    (feed_dir / "next").mkdir(parents=True)
    (feed_dir / "next" / "avito_feed.xml").write_text('<Ads formatVersion="3"><Ad><Id>1</Id></Ad><Ad><Id>2</Id></Ad></Ads>')
    base = ["--db", str(tmp_path / "t.db"), "--key-file", str(key_file), "--account", "1"]
    publish = ["avito-publish"] + base + ["--public-base", "https://panel.example", "--feed-root", str(tmp_path / "feed")]
    return tmp_path, token, base, publish


def make_live(tmp):
    conn = store.connect(tmp / "t.db")
    connections.set_live(conn, 1, True, NOW, "owner")
    conn.close()


def test_without_confirm_it_only_reads_and_prints_the_plan(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    http = ScriptedHttp(TOKEN, EMPTY_PROFILE)
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(publish) == 0
    out = capsys.readouterr().out
    assert "объявлений в черновике фида: 2" in out and "add" in out and "--confirm" in out
    assert not (tmp / "feed" / token / "avito_feed.xml").exists()
    assert "https://panel.example/feed/%s/avito_feed.xml" % token in out
    assert [c[0] for c in http.calls] == ["POST", "GET"]


def test_confirm_writes_the_profile_and_starts_one_upload_and_remembers_the_time(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    make_live(tmp)
    http = ScriptedHttp(TOKEN, EMPTY_PROFILE, (200, {}), (200, {}))
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(cli, "_epoch", lambda: 50_000.0)
    assert main(publish + ["--confirm"]) == 0
    assert (tmp / "feed" / token / "avito_feed.xml").read_text().count("<Ad>") == 2
    assert [(c[0], c[1].rsplit("/", 2)[-2:]) for c in http.calls[2:]] == [("POST", ["v2", "profile"]), ("POST", ["v1", "upload"])]
    conn = store.connect(tmp / "t.db")
    assert float(store.get_state(conn, "avito_last_upload_1")) == 50_000.0
    assert "загрузка запущена" in capsys.readouterr().out


def test_second_confirm_within_an_hour_does_not_upload_again(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    make_live(tmp)
    conn = store.connect(tmp / "t.db")
    store.set_state(conn, "avito_last_upload_1", "50000.0")
    conn.close()
    feed = {"feed_name": "tires", "feed_url": "https://panel.example/feed/%s/avito_feed.xml" % token}
    http = ScriptedHttp(TOKEN, (200, {"autoload_enabled": True, "feeds_data": [feed], "report_email": "", "schedule": []}))
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setattr(cli, "_epoch", lambda: 50_100.0)
    assert main(publish + ["--confirm"]) == 2
    assert "раза в час" in capsys.readouterr().err
    assert all("/upload" not in c[1] for c in http.calls)
    assert not (tmp / "feed" / token / "avito_feed.xml").exists()  # nothing was promoted or written


def test_missing_feed_file_or_keys_are_reported(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    (tmp / "feed" / token / "next" / "avito_feed.xml").unlink()
    assert main(publish) == 2 and "фид ещё не создан" in capsys.readouterr().err
    conn = store.connect(tmp / "t.db")
    conn.execute("DELETE FROM secret")
    conn.commit()
    conn.close()
    (tmp / "feed" / token / "next" / "avito_feed.xml").write_text("<Ads><Ad/></Ads>")
    assert main(publish) == 2 and "ключи аккаунта" in capsys.readouterr().err


def test_status_prints_counts_and_problems(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    upload = {"upload_id": 5, "status": "success_warning", "started_at": "2026-10-10T12:00:00", "stats": {"count": 2}, "events": []}
    items = {"items": [{"ad_id": "1", "avito_id": 11, "avito_status": "active", "messages": []},
                       {"ad_id": "2", "avito_id": None, "avito_status": "rejected", "messages": [{"title": "Нет модели", "description": "x"}]}],
             "meta": {"page": 1, "pages": 1, "perPage": 100, "total": 2}}
    http = ScriptedHttp(TOKEN, (404, {"error": {"code": 404, "message": "n"}}), (200, upload), (200, items))
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(["avito-status"] + base) == 0
    out = capsys.readouterr().out
    assert "success_warning" in out and "active: 1" in out and "rejected: 1" in out and "Нет модели" in out


def test_confirm_is_refused_while_publishing_is_switched_off_for_the_account(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    http = ScriptedHttp(TOKEN, EMPTY_PROFILE)
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(publish + ["--confirm"]) == 2
    assert "публикация аккаунта выключена" in capsys.readouterr().err
    assert http.calls == [] and not (tmp / "feed" / token / "avito_feed.xml").exists()


def test_a_shrunk_candidate_is_not_published_without_allow_shrink(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    make_live(tmp)
    (tmp / "feed" / token / "avito_feed.xml").write_text("".join(["<Ads>"] + ["<Ad><Id>%d</Id></Ad>" % i for i in range(10)] + ["</Ads>"]))
    http = ScriptedHttp(TOKEN, EMPTY_PROFILE)
    monkeypatch.setattr(cli, "_http", lambda: http)
    assert main(publish + ["--confirm"]) == 2
    assert "--allow-shrink" in capsys.readouterr().err
    assert all(c[0] == "GET" or c[1].endswith("/token/") for c in http.calls)


def test_feed_publish_makes_the_candidate_public_without_touching_avito(env, monkeypatch, capsys):
    tmp, token, base, publish = env
    monkeypatch.setattr(cli, "_http", lambda: pytest.fail("no network expected"))
    args = ["feed-publish", "--db", str(tmp / "t.db"), "--account", "1", "--feed-root", str(tmp / "feed")]
    assert main(args) == 0
    assert (tmp / "feed" / token / "avito_feed.xml").read_text().count("<Ad>") == 2
    assert "https://" not in capsys.readouterr().err


def test_feed_publish_records_the_published_state_of_that_account(env, monkeypatch, capsys):
    from datetime import date
    from helpers import catalog_xml, commodity_xml
    from tires_sync import cycle
    from tires_sync.catalog import parse_reference
    tmp, token, base, publish = env
    conn = store.connect(tmp / "t.db")
    ref = parse_reference('<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>')
    cycle.run_for_accounts(conn, catalog_xml(commodity_xml(NNOMMODIF="100")), ref, date(2026, 10, 10), tmp / "feed")
    conn.close()
    assert main(["feed-publish", "--db", str(tmp / "t.db"), "--account", "1", "--feed-root", str(tmp / "feed")]) == 0
    assert set(store.load_published(store.connect(tmp / "t.db"), account_id=1)) == {"100"}
    assert "состояние записано" in capsys.readouterr().out
