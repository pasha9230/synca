"""An ad the list of our ads does not know yet (2026-11-03, order 514: the ad was published after the nightly reading): «Создать заказ»
asks Avito for its Id once and adds the position; a failure is noted with the button «Подставить позицию из объявления», which repeats it."""
from __future__ import annotations

import re
from datetime import date, datetime

import pytest
from cryptography.fernet import Fernet

import panel_helpers
from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from test_order_search import REF, UNLOAD
from tires_sync import ad_stats, avito_stock, connections, orders, store
from tires_sync.avito import AvitoClient, RateLimited
from tires_sync.secretbox import SecretBox

NOW = "2026-10-12T09:00:00"
SNAPSHOT = "2026-10-12T01:21:40"                      # the last reading of the cabinet
TOKEN = (200, {"access_token": "t", "expires_in": 3600})
AD = 8544680058


def known(*pairs):
    return (200, {"items": [{"avito_id": a, "ad_id": i} for a, i in pairs]})


def client_for(*replies):
    http = ScriptedHttp(TOKEN, *replies)
    return AvitoClient("id", "secret", http=http, now=lambda: 0.0, sleep=lambda s: None), http


def base(tmp_path):
    conn = store.connect(str(tmp_path / "x.db"))
    account = connections.create_account(conn, "Шина-Колесо", NOW, "o")
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, status, seen_at, first_seen) VALUES (?,?,?,?,?,?,?)",
                 (account, 1, "100", "старое", "active", SNAPSHOT, SNAPSHOT))
    conn.commit()
    return conn, account


def at(text):
    return datetime.fromisoformat(text)


# ---- the lookup of one ad ---------------------------------------------------------------------------------------------------------------

def test_a_missed_ad_is_asked_once_and_kept_without_spoiling_the_snapshot_of_the_cabinet(tmp_path):
    conn, account = base(tmp_path)
    client, http = client_for(known((AD, "100-x4")))
    assert ad_stats.lookup_one(conn, client, account, str(AD), "Gislaved 285/60 R18", at(NOW)) == "100-x4"
    assert [c[1] for c in http.calls[1:]] == ["https://api.avito.ru/autoload/v2/items/ad_ids?query=%d" % AD]
    row = conn.execute("SELECT ad_id, status, seen_at, first_seen, title FROM avito_ad WHERE account_id = ? AND avito_id = ?", (account, AD)).fetchone()
    assert tuple(row) == ("100-x4", "", SNAPSHOT, NOW, "Gislaved 285/60 R18")
    assert avito_stock._items_of(conn, account) == {"100": [1]}            # the stock pass still sees the whole last reading


def test_an_ad_avito_does_not_call_ours_is_not_kept(tmp_path):
    conn, account = base(tmp_path)
    client, _ = client_for((200, {"items": []}))
    assert ad_stats.lookup_one(conn, client, account, AD, "", at(NOW)) is None
    assert conn.execute("SELECT COUNT(*) FROM avito_ad").fetchone()[0] == 1


def test_a_429_pauses_the_lookups_of_the_account_without_asking_again(tmp_path):
    conn, account = base(tmp_path)
    client, http = client_for((429, {"error": {"code": 429, "message": "Too many requests"}}))
    with pytest.raises(RateLimited):
        ad_stats.lookup_one(conn, client, account, AD, "", at(NOW))
    assert len(http.calls) == 2                                              # the token and one request: never repeated
    with pytest.raises(RateLimited):
        ad_stats.lookup_one(conn, client, account, AD, "", at("2026-10-12T09:04:00"))
    assert len(http.calls) == 2                                              # inside the pause nothing goes to Avito
    http.replies.append(known((AD, "100")))
    assert ad_stats.lookup_one(conn, client, account, AD, "", at("2026-10-12T09:05:01")) == "100"


def test_a_row_without_our_id_gets_it_and_the_next_reading_keeps_it_one_row(tmp_path):
    conn, account = base(tmp_path)
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, status, seen_at, first_seen) VALUES (?,?,NULL,'t','active',?,?)",
                 (account, AD, SNAPSHOT, SNAPSHOT))
    client, _ = client_for(known((AD, "100-x4")))
    ad_stats.lookup_one(conn, client, account, AD, "", at(NOW))
    client, http = client_for((200, {"resources": [{"id": AD, "title": "Gislaved", "price": 9000, "status": "active"}]}))
    assert ad_stats.collect(conn, client, account, date(2026, 10, 12), now_iso="2026-10-12T11:20:00", stats=False)["mapped"] == 0
    rows = conn.execute("SELECT ad_id, status, seen_at FROM avito_ad WHERE account_id = ? AND avito_id = ?", (account, AD)).fetchall()
    assert [tuple(r) for r in rows] == [("100-x4", "active", "2026-10-12T11:20:00")]
    assert all("/stats/" not in c[1] and "/accounts/self" not in c[1] for c in http.calls)     # the list only: no statistics, no «who are we»


# ---- «Создать заказ» and «Подставить позицию из объявления» -----------------------------------------------------------------------------

def panel(tmp_path, monkeypatch, http):
    monkeypatch.setattr(panel_helpers, "UNLOAD", UNLOAD)
    monkeypatch.setattr(panel_helpers, "REF_TEXT", REF)
    box = SecretBox(Fernet.generate_key())
    app = make_app(tmp_path, NOW=lambda: datetime(2026, 10, 12, 6, 0, 0), SECRET_BOX=box, AVITO_HTTP=http)
    client = app.test_client()
    tok = login(client)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, NOW)
    account = connections.create_account(conn, "Шина-Колесо", NOW, "o")
    for name in ("client_id", "client_secret"):
        connections.set_secret(conn, box, "avito", account, name, "x", NOW, "o")
    chat = conn.execute("INSERT INTO avito_chat (account_id, chat_id, item_id, item_title, peer_name) VALUES (?,?,?,?,?)",
                        (account, "c1", str(AD), "Ikon 205/55 R16", "Клиент")).lastrowid
    conn.commit()
    return app, client, tok, conn, chat


def card_of(conn, chat):
    return orders.get_card(conn, conn.execute("SELECT order_id FROM avito_chat WHERE id = ?", (chat,)).fetchone()[0])


def notes(conn, card_id):
    return [e["text"] for e in orders.list_events(conn, card_id) if "Позиция не определена" in e["text"]]


def test_a_deal_of_an_ad_published_after_the_reading_gets_its_position(tmp_path, monkeypatch):
    http = ScriptedHttp(TOKEN, known((AD, "100")))
    app, client, tok, conn, chat = panel(tmp_path, monkeypatch, http)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card = card_of(conn, chat)
    assert [(i["goods_id"], i["qty"]) for i in card["items"]] == [("100", 4)] and notes(conn, card["id"]) == []
    assert "Подставить позицию из объявления" not in client.get("/orders/%d" % card["id"]).get_data(as_text=True)


@pytest.mark.parametrize("reply,said", [((500, {"message": "down"}), "Авито ответил ошибкой 500"),
                                        ((429, {"error": {"code": 429, "message": "slow"}}), "Авито просит подождать (429)")])
def test_a_failed_lookup_is_noted_and_the_button_repeats_it_once(tmp_path, monkeypatch, reply, said):
    http = ScriptedHttp(TOKEN, reply)
    app, client, tok, conn, chat = panel(tmp_path, monkeypatch, http)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card = card_of(conn, chat)
    assert card["items"] == []
    assert notes(conn, card["id"]) == ["Позиция не определена: объявление Авито №%d не найдено среди наших объявлений (%s); %s" % (
        AD, said, "повторю по кнопке «Подставить позицию из объявления» в заказе")]
    page = client.get("/orders/%d" % card["id"]).get_data(as_text=True)
    assert "Подставить позицию из объявления</button>" in page and re.search(r'action="/orders/%d/ad-position"' % card["id"], page)
    conn.execute("DELETE FROM app_state WHERE key LIKE 'ad_lookup_pause:%'")
    conn.commit()
    http.replies[:] = [TOKEN, known((AD, "100"))]
    r = post(client, "/orders/%d/ad-position" % card["id"], tok)
    assert r.status_code == 302
    assert [i["goods_id"] for i in orders.get_card(conn, card["id"])["items"]] == ["100"]
    calls = len(http.calls)
    post(client, "/orders/%d/ad-position" % card["id"], tok)                 # a second press: no second position, nothing asked of Avito
    assert [i["goods_id"] for i in orders.get_card(conn, card["id"])["items"]] == ["100"] and len(http.calls) == calls
    assert conn.execute("SELECT COUNT(*) FROM avito_ad WHERE avito_id = ?", (AD,)).fetchone()[0] == 1


def test_the_button_of_a_failed_press_may_be_pressed_again_at_once(tmp_path, monkeypatch):
    http = ScriptedHttp(TOKEN, (500, {"message": "down"}))
    app, client, tok, conn, chat = panel(tmp_path, monkeypatch, http)
    post(client, "/chats/%d/create-deal" % chat, tok)
    card = card_of(conn, chat)
    http.replies[:] = [TOKEN, (500, {"message": "down"})]                  # each press is a client of its own: a token first
    post(client, "/orders/%d/ad-position" % card["id"], tok)
    assert len(notes(conn, card["id"])) == 2 and orders.get_card(conn, card["id"])["items"] == []
    http.replies[:] = [TOKEN, known((AD, "100"))]
    post(client, "/orders/%d/ad-position" % card["id"], tok)
    assert [i["goods_id"] for i in orders.get_card(conn, card["id"])["items"]] == ["100"]


def test_avito_is_not_asked_without_the_keys_of_the_account(tmp_path, monkeypatch):
    http = ScriptedHttp()                                                     # no reply scripted: any call to Avito fails the test
    app, client, tok, conn, chat = panel(tmp_path, monkeypatch, http)
    conn.execute("DELETE FROM secret")
    conn.commit()
    post(client, "/chats/%d/create-deal" % chat, tok)
    card = card_of(conn, chat)
    assert http.calls == [] and "у аккаунта нет ключей Авито" in notes(conn, card["id"])[0]


# ---- the list every 2 hours, the statistics once a day ----------------------------------------------------------------------------------

def test_avito_stats_collects_the_statistics_on_the_first_run_of_a_day_and_only_the_list_after(tmp_path, monkeypatch):
    from tires_sync import cli
    store.connect(tmp_path / "t.db").close()
    asked, reports = [], []
    monkeypatch.setattr(cli, "load_box", lambda key_file: object())
    monkeypatch.setattr(cli.connections, "list_feed_accounts", lambda conn: [{"id": 1, "name": "A"}])
    monkeypatch.setattr(cli.connections, "get_secret", lambda *a, **k: "x")
    monkeypatch.setattr(cli, "AvitoClient", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_collect_avito_reports", lambda conn, box: reports.append(1))
    fail = []

    def collect(conn, client, account_id, today, days=7, now_iso="", stats=True):
        asked.append(stats)
        if fail:
            raise OSError("timed out")
        return {"ads": 0, "mapped": 0, "days": 0}
    monkeypatch.setattr(cli.ad_stats, "collect", collect)
    run = ["avito-stats", "--db", str(tmp_path / "t.db")]
    fail.append(1)
    assert cli.main(run) == 1                                               # the statistics failed: the next run tries them again
    fail.clear()
    assert cli.main(run) == 0 and cli.main(run) == 0 and cli.main(run + ["--stats"]) == 0
    assert asked == [True, True, False, True] and len(reports) == 3         # Avito's upload report only with the statistics
