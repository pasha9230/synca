"""«Принудительная синхронизация»: после ручной публикации фида панель запускает загрузку на Авито (единственная запись в API Авито)."""
from __future__ import annotations

import re

from cryptography.fernet import Fernet

from helpers import catalog_xml, commodity_xml
from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from tires_sync import connections, store
from tires_sync.secretbox import SecretBox

NOW = "2026-10-07T10:00:00"
TOKEN = (200, {"access_token": "t", "expires_in": 3600})
STARTED = (200, {"upload_id": 7})


def goods(*ids):
    return catalog_xml(*[commodity_xml(NNOMMODIF=str(i), SMNFCODE="C%d" % i, NREST="12") for i in ids])


def setup(tmp_path, *replies, keys=True, live=True):
    http = ScriptedHttp(*replies)
    app = make_app(tmp_path, FEED_ROOT=str(tmp_path / "feed"), SECRET_BOX=SecretBox(Fernet.generate_key()), AVITO_HTTP=http)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    account = connections.create_account(conn, "Основной", NOW, "owner")
    connections.set_live(conn, account, live, NOW, "owner")
    conn.commit()
    if keys:
        post(client, "/connections/accounts/%d/keys" % account, tok, client_id="CLIENTID-12345678", client_secret="SECRETVALUE-XYZ")
        connections.set_avito_identity(conn, account, None, "Основной", "79000000000")         # as «Проверить подключение» records it
    open(app.config["UNLOAD_PATH"], "wb").write(goods(1, 2, 3, 4))
    return app, client, tok, conn, account, http


def confirm(client, tok, upload=True):
    rev = re.search(r'name="rev" value="([0-9a-f]+)"', client.get("/feed/update").get_data(as_text=True)).group(1)
    data = {"rev": rev}
    if upload:
        data["upload"] = "1"
    post(client, "/feed/update/confirm", tok, **data)
    return client.get("/publish/result").get_data(as_text=True)


def writes(http):
    return [c for c in http.calls if c[0] == "POST" and c[1].endswith("/autoload/v1/upload")]


def test_the_confirm_form_offers_the_upload_only_when_an_account_is_live(tmp_path):
    app, client, tok, conn, account, http = setup(tmp_path)
    assert 'name="upload" value="1" checked' in client.get("/feed/update").get_data(as_text=True)
    (tmp_path / "off").mkdir()
    app2, client2, tok2, conn2, acc2, http2 = setup(tmp_path / "off", live=False)
    assert 'name="upload"' not in client2.get("/feed/update").get_data(as_text=True)


def test_catalog_has_the_forced_sync_button(tmp_path):
    app, client, tok, conn, account, http = setup(tmp_path)
    html = client.get("/catalog").get_data(as_text=True)
    assert "Принудительная синхронизация" in html and 'action="/feed/update/fetch"' in html


def test_confirm_with_upload_publishes_our_feed_then_starts_the_avito_upload(tmp_path):
    app, client, tok, conn, account, http = setup(tmp_path, TOKEN, STARTED)
    html = confirm(client, tok)
    assert "загрузка на Авито запущена" in html and "SECRETVALUE" not in html
    assert len(writes(http)) == 1 and [c[0] for c in http.calls] == ["POST", "POST"]
    assert store.get_state(conn, "avito_last_upload_%d" % account)
    actions = [r[0] for r in conn.execute("SELECT action FROM audit")]
    assert "avito_upload_started" in actions and "feed_updated_manually" in actions


def test_without_the_checkbox_nothing_is_sent_to_avito(tmp_path):
    app, client, tok, conn, account, http = setup(tmp_path)
    html = confirm(client, tok, upload=False)
    assert "Опубликовано" in html and http.calls == []
    assert store.get_state(conn, "avito_last_upload_%d" % account) is None


def test_a_second_upload_within_an_hour_is_not_sent_but_the_feed_is_still_published(tmp_path):
    app, client, tok, conn, account, http = setup(tmp_path, TOKEN, STARTED)
    confirm(client, tok)
    open(app.config["UNLOAD_PATH"], "wb").write(goods(1, 2, 3, 5))
    html = confirm(client, tok)
    assert "не чаще раза в час" in html and "Фид у нас обновлён" in html
    assert len(writes(http)) == 1


def test_avito_refusal_is_reported_and_does_not_undo_the_published_feed(tmp_path):
    app, client, tok, conn, account, http = setup(tmp_path, TOKEN, (500, {"error": {"code": 500, "message": "boom SECRETVALUE-XYZ"}}))
    html = confirm(client, tok)
    assert "загрузка не запущена" in html and "Опубликовано: Основной" in html
    assert "SECRETVALUE" not in html and "***" in html
    assert "SECRETVALUE" not in str(list(conn.execute("SELECT * FROM audit")))
    assert not store.get_state(conn, "avito_last_upload_%d" % account)  # the hour is given back after a failure
    assert "avito_upload_refused" in [r[0] for r in conn.execute("SELECT action FROM audit")]


def test_missing_keys_are_explained_and_nothing_is_called(tmp_path):
    app, client, tok, conn, account, http = setup(tmp_path, keys=False)
    html = confirm(client, tok)
    assert "ключи доступа Авито не заданы" in html and http.calls == []


def test_an_account_that_is_not_live_gets_no_upload(tmp_path):
    app, client, tok, conn, account, http = setup(tmp_path, live=False)
    rev = re.search(r'name="rev" value="([0-9a-f]+)"', client.get("/feed/update").get_data(as_text=True)).group(1)
    post(client, "/feed/update/confirm", tok, rev=rev, upload="1")
    html = client.get("/publish/result").get_data(as_text=True)
    assert "ни один фид не опубликован" in html and http.calls == []


def test_the_panel_client_for_normal_checks_stays_read_only(tmp_path):
    from tires_sync.avito import AvitoClient, WriteDisabled
    import pytest
    with pytest.raises(WriteDisabled):
        AvitoClient("a", "b", http=ScriptedHttp(), now=lambda: 0.0).start_upload()


def test_a_network_error_on_avito_does_not_break_the_confirmation(tmp_path):
    def down(*args):
        raise OSError("network is down")

    app, client, tok, conn, account, http = setup(tmp_path)
    app.config["AVITO_HTTP"] = down
    html = confirm(client, tok)
    assert "Опубликовано: Основной" in html and "Авито недоступно" in html
    assert store.get_state(conn, "avito_last_upload_%d" % account) in (None, "")
    assert "avito_upload_refused" in [r[0] for r in conn.execute("SELECT action FROM audit")]


def test_a_broken_or_old_stamp_does_not_block_and_a_rate_limit_gives_the_hour_back(tmp_path):
    app, client, tok, conn, account, http = setup(tmp_path, TOKEN, STARTED)
    store.set_state(conn, "avito_last_upload_%d" % account, "not-a-number")
    assert "загрузка на Авито запущена" in confirm(client, tok)
    app2_dir = tmp_path / "b"
    app2_dir.mkdir()
    app2, client2, tok2, conn2, acc2, http2 = setup(app2_dir, TOKEN, (429, {"error": {"code": 429, "message": "slow"}}, {"Retry-After": "1"}))
    store.set_state(conn2, "avito_last_upload_%d" % acc2, "1.0")  # long ago
    html = confirm(client2, tok2)
    assert "Авито отклонило запуск" in html
    assert store.get_state(conn2, "avito_last_upload_%d" % acc2) == "1.0"  # the hour is given back after a failure


def test_one_failing_account_does_not_stop_the_next(tmp_path):
    app, client, tok, conn, account, http = setup(tmp_path, TOKEN, (500, {"error": {"code": 500, "message": "boom"}}), TOKEN, STARTED)
    second = connections.create_account(conn, "Второй", NOW, "owner")
    connections.set_live(conn, second, True, NOW, "owner")
    conn.commit()
    post(client, "/connections/accounts/%d/keys" % second, tok, client_id="CLIENTID-2222222", client_secret="SECRET-2222")
    connections.set_avito_identity(conn, second, None, "Второй", "79110000000")
    html = confirm(client, tok)
    assert "загрузка не запущена" in html and "загрузка на Авито запущена" in html
    assert len(writes(http)) == 2


def test_without_a_master_key_the_upload_is_skipped_with_a_reason(tmp_path):
    app, client, tok, conn, account, http = setup(tmp_path)
    app.config["SECRET_BOX"] = None
    html = confirm(client, tok)
    assert "мастер-ключ недоступен" in html and http.calls == []
