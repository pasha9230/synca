"""Avito's messenger webhook: a secret address per account, the pushed message kept as the reading keeps it, nothing sent, the reading
after it adds nothing; the commands show before they write."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from http_fakes import ScriptedHttp
from panel_helpers import login, make_app
from test_chats_sync import TS, FakeClient, chat, msg
from tires_sync import avito_webhook, chat_deals, chats, cli, connections, orders, store
from tires_sync.cli import main
from tires_sync.panel.gunicorn_logger import mask
from tires_sync.secretbox import create_key_file, load_box

NOW = "2026-10-13T12:00:00"
TOKEN = (200, {"access_token": "t", "expires_in": 3600})


def notification(message_id="m2", text="Есть в наличии?", chat_id="c1", author=2, user=1, created=TS + 10, kind="text", content=None):
    return {"id": "evt-" + message_id, "version": "v3.0.0", "timestamp": created,
            "payload": {"type": "message", "value": {"id": message_id, "chat_id": chat_id, "user_id": user, "author_id": author, "created": created,
                                                     "type": kind, "chat_type": "u2i", "content": content if content is not None else {"text": text},
                                                     "item_id": 111, "published_at": "2026-10-13T12:00:10Z", "read": None}}}


@pytest.fixture
def env(tmp_path):
    """A panel whose account reads chats and has one chat c1 (read once: the past), bound to an open order; the webhook's secret."""
    app = make_app(tmp_path, AVITO_HOOK_RUN=lambda work: work(), AVITO_HTTP=ScriptedHttp())       # no reply scripted: any call to Avito fails the test
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    account = connections.create_account(conn, "Основной", NOW, "o")
    connections.set_modes(conn, account, True, True, NOW, "o")
    reader = FakeClient([[chat("c1", text="Здравствуйте")]], {"c1": [msg("m1", "Здравствуйте", read=1)]})
    chats.sync_account(conn, reader, account, NOW)
    conn.execute("UPDATE avito_chat SET unread = 0")
    conn.commit()
    [row] = chats.list_chats(conn)
    card = orders.create_card(conn, dict(client_name="Татьяна"), NOW, "o")
    chat_deals.link_chat(conn, row["id"], card, NOW, "owner")
    secret = avito_webhook.new_secret(conn, account, NOW)
    return app, conn, account, secret, row["id"], card, reader


def hook(app, secret, body, raw=None):
    data = raw if raw is not None else json.dumps(body)
    return app.test_client().post("/hook/avito/%s" % secret, data=data, content_type="application/json")


def messages(conn, chat_row):
    return [(m["message_id"], m["direction"], m["text"]) for m in chats.list_messages(conn, chat_row)]


def client_wrote(conn, card):
    return [r[0] for r in conn.execute("SELECT text FROM order_event WHERE order_id = ? AND text LIKE 'Клиент написал:%'", (card,))]


def test_a_pushed_message_goes_into_its_chat_at_once_with_one_alert(env):
    app, conn, account, secret, chat_row, card, _reader = env
    r = hook(app, secret, notification())
    assert (r.status_code, r.get_data(as_text=True)) == (200, "ok") and "Set-Cookie" not in r.headers
    assert messages(conn, chat_row) == [("m1", "in", "Здравствуйте"), ("m2", "in", "Есть в наличии?")]
    got = chats.get_chat(conn, chat_row)
    assert (got["last_text"], got["last_dir"], got["unread"]) == ("Есть в наличии?", "in", 1)
    assert client_wrote(conn, card) == ["Клиент написал: Есть в наличии?"]
    assert avito_webhook.state(conn, account, dt.datetime(2026, 10, 13, 12, 0))["last_event_at"] == "2026-10-07T15:00:00"      # the panel's clock in Moscow time
    assert app.config["AVITO_HTTP"].calls == []                                              # nothing went to Avito: no client got anything


def test_a_repeat_of_the_same_message_is_kept_once(env):
    app, conn, account, secret, chat_row, card, _reader = env
    for _ in range(3):
        assert hook(app, secret, notification()).status_code == 200
    assert [m for m, _d, _t in messages(conn, chat_row)] == ["m1", "m2"]
    assert conn.execute("SELECT COUNT(*), MIN(result) FROM avito_webhook_event").fetchone() == (1, "stored")
    assert len(client_wrote(conn, card)) == 1


def test_the_reading_after_the_webhook_adds_no_second_copy_nor_a_second_alert(env):
    app, conn, account, secret, chat_row, card, reader = env
    hook(app, secret, notification())
    reader.chat_pages = [[chat("c1", updated=TS + 10, text="Есть в наличии?")]]
    reader.messages["c1"] = [msg("m1", "Здравствуйте", read=1), msg("m2", "Есть в наличии?", created=TS + 10)]
    assert chats.sync_account(conn, reader, account, "2026-10-13T12:05:00")["messages"] == 0
    assert [m for m, _d, _t in messages(conn, chat_row)] == ["m1", "m2"]
    assert len(client_wrote(conn, card)) == 1


def test_our_own_message_pushed_back_is_ours_and_clears_the_waiting(env):
    app, conn, account, secret, chat_row, card, _reader = env
    hook(app, secret, notification())
    hook(app, secret, notification("m3", "Да, есть", author=1, created=TS + 20))
    assert messages(conn, chat_row)[-1] == ("m3", "out", "Да, есть")
    got = chats.get_chat(conn, chat_row)
    assert (got["last_dir"], got["unread"]) == ("out", 0)
    assert len(client_wrote(conn, card)) == 1


def test_a_chat_the_panel_does_not_have_is_left_to_the_reading(env):
    app, conn, account, secret, chat_row, card, _reader = env
    assert hook(app, secret, notification("n1", "Привет", chat_id="c-new")).status_code == 200
    assert len(chats.list_chats(conn)) == 1
    assert conn.execute("SELECT result FROM avito_webhook_event WHERE message_id = 'n1'").fetchone() == ("no_chat",)


def test_a_wrong_secret_or_an_account_that_does_not_read_chats_is_404_and_nothing_is_kept(env):
    app, conn, account, secret, chat_row, card, _reader = env
    for bad in ("x" * 43, secret[:-1] + ("A" if secret[-1] != "A" else "B"), "short", "%2e%2e", ""):
        r = hook(app, bad, notification())
        assert (r.status_code, r.get_data(as_text=True)) == (404, "") or bad in ("", "%2e%2e") and r.status_code == 404
    connections.set_modes(conn, account, False, True, NOW, "o")                             # «Читать чаты» off
    assert hook(app, secret, notification()).status_code == 404
    assert [m for m, _d, _t in messages(conn, chat_row)] == ["m1"]
    assert conn.execute("SELECT COUNT(*) FROM avito_webhook_event").fetchone() == (0,)


def test_another_cabinet_in_the_body_is_not_kept(env):
    app, conn, account, secret, chat_row, card, _reader = env
    assert hook(app, secret, notification(user=777, author=2)).status_code == 200
    assert [m for m, _d, _t in messages(conn, chat_row)] == ["m1"]


def test_avitos_check_garbage_and_other_kinds_are_200_and_change_nothing(env):
    app, conn, account, secret, chat_row, card, _reader = env
    for raw in ("{}", "not json", "[1,2]", json.dumps({"payload": {"type": "other", "value": {"id": "z", "chat_id": "c1"}}}),
                json.dumps({"payload": {"type": "message", "value": {"id": "z"}}}), json.dumps({"payload": {"type": "message", "value": {"id": "z", "chat_id": "c1", "content": "x"}}})):
        assert hook(app, secret, None, raw=raw).status_code == 200
    assert [m for m, _d, _t in messages(conn, chat_row)] == ["m1"]
    assert conn.execute("SELECT COUNT(*) FROM avito_webhook_event").fetchone() == (0,)


def test_a_big_body_is_refused_and_only_post_is_accepted(env):
    app, conn, account, secret, chat_row, card, _reader = env
    big = notification(text="я" * (avito_webhook.MAX_BODY))
    assert hook(app, secret, big).status_code == 413
    assert [m for m, _d, _t in messages(conn, chat_row)] == ["m1"]
    assert app.test_client().get("/hook/avito/%s" % secret).status_code == 405


def test_a_body_without_its_length_is_still_cut(env):
    app, conn, account, secret, chat_row, card, _reader = env
    status, _text = avito_webhook.handle(conn, secret, b"{" * (avito_webhook.MAX_BODY + 1), NOW)
    assert status == 413


def test_a_call_and_a_photo_are_kept_as_the_reading_keeps_them(env):
    app, conn, account, secret, chat_row, card, _reader = env
    hook(app, secret, notification("k1", kind="call", content={"call": {"status": "missed", "target_user_id": 1}}))
    url = "https://img.k.avito.ru/chat/1280x960/a.jpg"
    hook(app, secret, notification("p1", kind="image", created=TS + 30, content={"image": {"sizes": {"1280x960": url, "32x32": "https://img.k.avito.ru/chat/32x32/a.jpg"}}}))
    assert ("k1", "in", "Звонок через Авито: пропущен") in messages(conn, chat_row)
    assert conn.execute("SELECT media FROM avito_message WHERE message_id = 'p1'").fetchone() == (url,)
    assert chats.get_chat(conn, chat_row)["last_text"] == "Фото"
    assert [r[0] for r in conn.execute("SELECT text FROM order_event WHERE order_id = ? AND text LIKE 'Клиент звонил:%'", (card,))] == ["Клиент звонил: Звонок через Авито: пропущен"]


def test_a_chat_deleted_in_the_panel_gets_nothing(env):
    app, conn, account, secret, chat_row, card, _reader = env
    from tires_sync import trash
    trash.delete_chat(conn, chat_row, NOW, "owner")
    hook(app, secret, notification())
    assert conn.execute("SELECT COUNT(*) FROM avito_message WHERE message_id = 'm2'").fetchone() == (0,)


def test_a_new_secret_closes_the_old_address(env):
    app, conn, account, secret, chat_row, card, _reader = env
    fresh = avito_webhook.new_secret(conn, account, NOW)
    assert fresh != secret and len(fresh) >= 40
    assert hook(app, secret, notification()).status_code == 404
    assert hook(app, fresh, notification()).status_code == 200
    assert secret not in json.dumps(conn.execute("SELECT * FROM avito_webhook").fetchall())    # only the hash is kept


def test_the_access_log_hides_the_secret():
    assert mask("POST /hook/avito/AbC_123-xyz HTTP/1.1") == "POST /hook/avito/*** HTTP/1.1"


def test_the_account_page_shows_the_state(env):
    app, conn, account, secret, chat_row, card, _reader = env
    client = app.test_client()
    login(client)
    html = client.get("/connections/avito/%d" % account).get_data(as_text=True)
    assert "Вебхук Авито: не подписан" in html
    avito_webhook.mark_subscribed(conn, account, "2026-10-13T12:00:00")
    hook(app, secret, notification())
    html = client.get("/connections/avito/%d" % account).get_data(as_text=True)
    assert "Вебхук Авито: подписан с 13.10.2026 12:00" in html and secret not in html


# ---- the commands --------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "t.db")
    account = connections.create_account(conn, "Основной", NOW, "o")
    key_file = tmp_path / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_secret(conn, box, "avito", account, "client_id", "CLIENTID-12345678", NOW, "o")
    connections.set_secret(conn, box, "avito", account, "client_secret", "SECRETVALUE-XYZ", NOW, "o")
    connections.set_modes(conn, account, True, True, NOW, "o")
    http = ScriptedHttp()
    monkeypatch.setattr(cli, "_http", lambda: http)
    monkeypatch.setenv("TIRES_PUBLIC_BASE", "https://panel.example.com")                  # the panel's address outside, a setting
    return conn, account, http, ["--db", str(tmp_path / "t.db"), "--key-file", str(key_file)]


BASE = "https://panel.example.com/hook/avito/"


def test_subscribe_without_confirm_calls_nobody_and_writes_nothing(cli_env, capsys):
    conn, account, http, base = cli_env
    assert main(["avito-webhook-subscribe", "--account", str(account)] + base) == 0
    assert http.calls == []
    assert conn.execute("SELECT COUNT(*) FROM avito_webhook").fetchone() == (0,)
    assert "Ничего не записано" in capsys.readouterr().out


class FakeAvito:
    """Avito's messenger subscriptions as a list: a subscription adds an address, an unsubscription removes it."""

    def __init__(self, urls):
        self.urls, self.calls = list(urls), []

    def __call__(self, method, url, headers, body):
        path = url.split("avito.ru")[1]
        sent = json.loads(body)["url"] if path.startswith("/messenger/v") and body else None
        self.calls.append((method, path, sent))
        if path == "/token/":
            return 200, {}, json.dumps(TOKEN[1]).encode()
        if path == "/messenger/v3/webhook":
            self.urls.append(sent)
        elif path == "/messenger/v1/webhook/unsubscribe":
            self.urls.remove(sent)
        elif path != "/messenger/v1/subscriptions":
            return 404, {}, b"{}"
        return 200, {}, json.dumps({"subscriptions": [{"url": u, "version": "3"} for u in self.urls]} if path.endswith("subscriptions") else {"ok": True}).encode()


def test_subscribe_with_confirm_subscribes_replaces_our_old_address_and_keeps_others(cli_env, monkeypatch, capsys):
    conn, account, _http, base = cli_env
    other = "https://crm.example.ru/avito"
    avito = FakeAvito([BASE + "old-secret-0123456789", other])
    monkeypatch.setattr(cli, "_http", lambda: avito)
    assert main(["avito-webhook-subscribe", "--account", str(account), "--confirm"] + base) == 0
    [new] = [u for m, p, u in avito.calls if p == "/messenger/v3/webhook"]
    assert new.startswith(BASE)
    assert [c[1:] for c in avito.calls if c[1] == "/messenger/v1/webhook/unsubscribe"] == [("/messenger/v1/webhook/unsubscribe", BASE + "old-secret-0123456789")]
    assert avito.urls == [other, new]
    secret = new[len(BASE):]
    assert avito_webhook.account_of(conn, secret) == account and avito_webhook.state(conn, account, dt.datetime(2026, 10, 13, 12, 0))["subscribed"]
    out = capsys.readouterr().out
    assert secret not in out and BASE + "***" in out


def test_subscribe_refuses_an_account_that_does_not_read_chats(cli_env, capsys):
    conn, account, http, base = cli_env
    connections.set_modes(conn, account, False, True, NOW, "o")                             # «Читать чаты» off
    assert main(["avito-webhook-subscribe", "--account", str(account), "--confirm"] + base) == 2
    assert http.calls == []


def test_the_panel_address_is_a_setting_and_without_one_nothing_is_done(cli_env, capsys, monkeypatch):
    conn, account, http, base = cli_env
    monkeypatch.delenv("TIRES_PUBLIC_BASE")
    assert main(["avito-webhook-subscribe", "--account", str(account), "--confirm"] + base) == 2
    assert http.calls == [] and "TIRES_PUBLIC_BASE" in capsys.readouterr().err
    assert main(["avito-webhook-status"] + base) == 2 and http.calls == []
    store.set_state(conn, "public_base", "https://base.example.com")                       # the base's address of the panel
    assert main(["avito-webhook-subscribe", "--account", str(account)] + base) == 0
    assert "https://base.example.com/hook/avito/" in capsys.readouterr().out
    monkeypatch.setenv("TIRES_PUBLIC_BASE", "https://env.example.com")                     # the environment over the base
    assert main(["avito-webhook-subscribe", "--account", str(account)] + base) == 0
    assert "https://env.example.com/hook/avito/" in capsys.readouterr().out
    assert main(["avito-webhook-subscribe", "--account", str(account), "--base-url", "https://arg.example.com"] + base) == 0
    assert "https://arg.example.com/hook/avito/" in capsys.readouterr().out                # the argument over both
    monkeypatch.setenv("TIRES_PUBLIC_BASE", "http://env.example.com")
    assert main(["avito-webhook-subscribe", "--account", str(account), "--confirm"] + base) == 2
    assert http.calls == [] and "только https://" in capsys.readouterr().err


def test_unsubscribe_shows_first_then_removes_only_ours_and_forgets_the_secret(cli_env, capsys):
    conn, account, http, base = cli_env
    secret = avito_webhook.new_secret(conn, account, NOW)
    avito_webhook.mark_subscribed(conn, account, NOW)
    listing = (200, {"subscriptions": [{"url": BASE + secret, "version": "3"}, {"url": "https://crm.example.ru/avito", "version": "3"}]})
    http.replies.extend([TOKEN, listing])
    assert main(["avito-webhook-unsubscribe", "--account", str(account)] + base) == 0
    assert [u.split("avito.ru")[1] for _m, u, _h, _b in http.calls] == ["/token/", "/messenger/v1/subscriptions"]
    assert avito_webhook.account_of(conn, secret) == account
    http.calls.clear()
    http.replies.extend([TOKEN, listing, (200, {"ok": True})])
    assert main(["avito-webhook-unsubscribe", "--account", str(account), "--confirm"] + base) == 0
    _m, url, _h, body = http.calls[2]
    assert (url.split("avito.ru")[1], json.loads(body)["url"]) == ("/messenger/v1/webhook/unsubscribe", BASE + secret)
    assert len(http.calls) == 3
    assert avito_webhook.account_of(conn, secret) is None and not avito_webhook.state(conn, account, dt.datetime(2026, 10, 13, 12, 0))["subscribed"]
    assert secret not in capsys.readouterr().out


def test_status_reads_only_and_hides_the_secret(cli_env, capsys):
    conn, account, http, base = cli_env
    secret = avito_webhook.new_secret(conn, account, NOW)
    http.replies.extend([TOKEN, (200, {"subscriptions": [{"url": BASE + secret, "version": "3"}]})])
    assert main(["avito-webhook-status"] + base) == 0
    out = capsys.readouterr().out
    assert secret not in out and BASE + "*** (наш)" in out
    assert [m for m, _u, _h, _b in http.calls] == ["POST", "POST"]           # the token and Avito's list (a read, though a POST)


def test_a_deeply_nested_body_is_not_a_server_error(env):
    app, conn, account, secret, chat_row, card, _reader = env
    assert hook(app, secret, None, raw="[" * 60000).status_code == 200
    assert hook(app, secret, None, raw='{"payload":' * 5000).status_code == 200


def test_a_failure_in_the_route_never_writes_the_secret_to_the_log(env, monkeypatch, caplog):
    import sqlite3
    app, conn, account, secret, chat_row, card, _reader = env
    app.config["PROPAGATE_EXCEPTIONS"] = False                                    # as on the server: Flask logs the failure itself
    monkeypatch.setattr(avito_webhook, "account_of", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    with caplog.at_level("DEBUG"):
        r = hook(app, secret, notification())
    assert r.status_code == 503
    assert caplog.records and not any(secret in (rec.getMessage() + str(rec.exc_text or "")) for rec in caplog.records)


def test_a_failure_after_the_answer_keeps_nothing_half_done(env, monkeypatch):
    app, conn, account, secret, chat_row, card, _reader = env
    app.config["AVITO_HOOK_RUN"] = lambda work: work()
    monkeypatch.setattr(chats, "_note_client_wrote", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert hook(app, secret, notification()).status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM avito_webhook_event").fetchone() == (0,)          # rolled back: Avito's repeat or the reading brings it
    assert [m for m, _d, _t in messages(conn, chat_row)] == ["m1"]


def test_a_chat_set_aside_opens_again_with_one_alert_and_a_waiting_chat_gets_no_second(env):
    app, conn, account, secret, chat_row, card, _reader = env
    chats.set_status(conn, chat_row, "closed", NOW)
    hook(app, secret, notification())
    assert chats.get_chat(conn, chat_row)["status"] == "open"
    hook(app, secret, notification("m3", "Алло?", created=TS + 20))
    assert client_wrote(conn, card) == ["Клиент написал: Есть в наличии?"]


def test_the_webhook_after_the_reading_adds_nothing(env):
    app, conn, account, secret, chat_row, card, reader = env
    reader.chat_pages = [[chat("c1", updated=TS + 10, text="Есть в наличии?")]]
    reader.messages["c1"] = [msg("m1", "Здравствуйте", read=1), msg("m2", "Есть в наличии?", created=TS + 10)]
    chats.sync_account(conn, reader, account, "2026-10-13T12:05:00")
    hook(app, secret, notification())
    assert [m for m, _d, _t in messages(conn, chat_row)] == ["m1", "m2"]
    assert conn.execute("SELECT result FROM avito_webhook_event").fetchone() == ("known",)
    assert len(client_wrote(conn, card)) == 1


def test_a_message_without_its_time_is_left_to_the_reading(env):
    app, conn, account, secret, chat_row, card, _reader = env
    body = notification()
    body["payload"]["value"]["created"] = "2026-10-13"
    assert hook(app, secret, body).status_code == 200
    assert [m for m, _d, _t in messages(conn, chat_row)] == ["m1"]
