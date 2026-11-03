"""The price on Avito right after «Применить» (price_push): the client's update_price, the queue from the published feed, the sender
(pause, 429, the keys, the switch), the check mode, the price equal to the next feed's, the gate. A fake Avito, never the network."""
from __future__ import annotations

import io
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import pytest

from helpers import catalog_xml, commodity_xml
from http_fakes import ScriptedHttp
from panel_helpers import login, make_app, post
from tires_sync import connections, price_push, staging, store
from tires_sync.avito import AvitoClient, AvitoError, WriteDisabled
from tires_sync.catalog import parse_reference
from tires_sync.cycle import run_for_accounts
from tires_sync.panel.routes_drafts import publish_summary
from tires_sync.settings_store import active_settings, confirm_draft, create_draft
from tires_sync.suppliers import load_all_parsed

T = "2026-10-07T10:00:00"
NOW = datetime(2026, 11, 3, 12, 0, 0)
ROWS = tuple(commodity_xml(NNOMMODIF=str(i), NREST="12", SMNFCODE="C%d" % i) for i in range(1, 7))
TOKEN = (200, {"access_token": "t", "expires_in": 3600})
AVITO = {str(i): 100 + i for i in range(1, 7)}          # our Id → the Avito ad


class Avito:
    """Avito as the price method sees it: `answers` is a queue of answers for the next update_price calls (None: success)."""

    def __init__(self, answers=(), token=200):
        self.answers = list(answers)
        self.token = token
        self.calls = []
        self.prices = {}

    def __call__(self, method, url, headers, body):
        path = urlparse(url).path
        self.calls.append((method, path))
        if path == "/token/":
            if self.token != 200:
                return self.token, {}, json.dumps({"error": "invalid_client"}).encode()
            return 200, {}, json.dumps({"access_token": "t", "expires_in": 3600}).encode()
        found = re.fullmatch(r"/core/v1/items/(\d+)/update_price", path)
        assert found and method == "POST", (method, url)
        answer = self.answers.pop(0) if self.answers else None
        if answer is not None:
            return answer
        self.prices[int(found.group(1))] = json.loads(body)["price"]
        return 200, {}, json.dumps({"result": {"success": True}}).encode()

    def price_calls(self):
        return [c for c in self.calls if c[1] != "/token/"]


class Box:
    """The secret box: the keys of every account are there."""

    def decrypt(self, value):
        return value


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(price_push, "PAUSE", 0.0)


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setattr(connections, "get_secret", lambda conn, box, kind, owner, name: "k-%s" % name)


class Popen:
    def __init__(self):
        self.started = []

    def __call__(self, args, **kwargs):
        self.started.append(args)
        return type("P", (), {"pid": 4242, "poll": lambda self: None})()


def world(tmp_path, mode="on", ads=AVITO):
    feed_root = tmp_path / "feed"
    popen = Popen()
    app = make_app(tmp_path, FEED_ROOT=str(feed_root), POPEN=popen)
    client = app.test_client()
    tok = login(client)
    open(app.config["UNLOAD_PATH"], "wb").write(catalog_xml(*ROWS))
    conn = store.connect(app.config["DB_PATH"])
    confirm_draft(conn, create_draft(conn, {"guard_min": 50, "guard_fraction": 0.0}, T), T, "owner")
    connections.create_account(conn, "Основной", T, "owner")
    connections.set_live(conn, 1, True, T, "owner")
    for ad_id, avito_id in ads.items():
        conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, status, seen_at) VALUES (1,?,?,'','active',?)", (avito_id, ad_id, T))
    conn.commit()
    staging.set_override(conn, "6", "off", T)                       # a first «Применить»: the published feed Avito already has
    post(client, "/draft/%d/confirm" % staging.summary(conn)["draft_id"], tok)
    price_push.save_mode(conn, 1, mode, T, "owner")
    folder = feed_root / connections.get_account(conn, 1)["feed_token"]
    return app, client, tok, conn, folder, popen


def apply_own_price(client, tok, conn, goods_id="1", price=5500):
    staging.set_own_price(conn, goods_id, [1], {"price": price}, T)
    return post(client, "/draft/%d/confirm" % staging.summary(conn)["draft_id"], tok)


def queue(conn):
    return {r[0]: r[1:] for r in conn.execute("SELECT ad_id, avito_id, price, old_price, status, error FROM avito_price ORDER BY ad_id")}


def run(conn, folder, fake, sleeps=None):
    out = io.StringIO()
    done = price_push.run(conn, Box(), folder.parent, fake, now=NOW, sleep=(sleeps.append if sleeps is not None else (lambda s: None)), out=out)
    return done, out.getvalue()


def live_prices(folder):
    return price_push.feed_prices(folder / "avito_feed.xml")


# ---- the client ------------------------------------------------------------------------------------------------------------------------

def test_update_price_is_a_write_with_the_documented_path_and_body():
    http = ScriptedHttp(TOKEN)
    with pytest.raises(WriteDisabled):
        AvitoClient("id", "secret", http=http).update_price(7, 100)
    assert http.calls == []
    http = ScriptedHttp(TOKEN, (200, {"result": {"success": True}}))
    AvitoClient("id", "secret", http=http, now=lambda: 0.0, allow_write=True).update_price(7, 4900)
    method, url, _headers, body = http.calls[1]
    assert (method, urlparse(url).path, json.loads(body)) == ("POST", "/core/v1/items/7/update_price", {"price": 4900})
    for bad in (0, -5, 10.5, True):
        with pytest.raises(ValueError):
            AvitoClient("id", "secret", http=ScriptedHttp(), allow_write=True).update_price(7, bad)


def test_update_price_waits_as_retry_after_says_on_429_and_takes_no_unconfirmed_answer():
    sleeps = []
    http = ScriptedHttp(TOKEN, (429, {}, {"Retry-After": "2"}), (200, {"result": {"success": True}}))
    AvitoClient("id", "secret", http=http, now=lambda: 0.0, sleep=sleeps.append, allow_write=True).update_price(7, 4900)
    assert [c[0] for c in http.calls] == ["POST", "POST", "POST"] and 2.0 in sleeps
    http = ScriptedHttp(TOKEN, (200, {"result": {"success": False}}))
    with pytest.raises(AvitoError):
        AvitoClient("id", "secret", http=http, now=lambda: 0.0, allow_write=True).update_price(7, 4900)


# ---- the queue after «Применить» ---------------------------------------------------------------------------------------------------------

def test_a_changed_price_is_queued_from_the_published_feed_and_the_sender_starts_apart(tmp_path):
    app, client, tok, conn, folder, popen = world(tmp_path)
    before = live_prices(folder)
    r = apply_own_price(client, tok, conn)
    assert r.headers["Location"].endswith("/publish/result")
    after = live_prices(folder)
    assert after["1"] == 5500 != before["1"]
    rows = queue(conn)
    assert rows == {"1": (101, 5500, before["1"], "queued", "")}            # only the changed ad; the others kept their price
    assert len(popen.started) == 1 and popen.started[0][3:] == ["price-push", "--db", app.config["DB_PATH"], "--feed-root", str(folder.parent),
                                                          "--wait", "1200"]
    assert "Цены на Авито: отправляются сразу, объявлений 1." in client.get("/publish/result").get_data(as_text=True)


def test_the_sent_price_is_the_one_of_the_feed_and_the_next_cycle_builds_the_same(tmp_path):
    app, client, tok, conn, folder, _ = world(tmp_path)
    apply_own_price(client, tok, conn, price=5555)                      # an own price goes up to tens: 5560
    fake = Avito()
    done, _ = run(conn, folder, fake)
    assert done == {1: {"sent": 1, "failed": 0, "not_sent": 0, "skipped": 0}}
    assert fake.prices == {101: live_prices(folder)["1"]} and fake.prices[101] == 5560
    ref = parse_reference(open(app.config["REFERENCE_PATH"], encoding="utf-8").read())
    parsed = load_all_parsed(conn, active_settings(conn), Path(app.config["UNLOAD_PATH"]))[0]
    results = run_for_accounts(conn, None, ref, date(2026, 10, 7), folder.parent, input_sha="x", parsed=parsed)   # the next cycle
    assert all(r.ok for r in results)
    assert price_push.feed_prices(folder / "next" / "avito_feed.xml")["1"] == fake.prices[101]
    assert queue(conn)["1"][3] == "sent"
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'avito_price_push'").fetchone()[0] == 1


def test_an_unchanged_publication_queues_nothing_and_starts_nothing(tmp_path):
    _, client, tok, conn, folder, popen = world(tmp_path)
    staging.set_override(conn, "2", "on", T)                            # a change that moves no price
    post(client, "/draft/%d/confirm" % staging.summary(conn)["draft_id"], tok)
    assert queue(conn) == {} and popen.started == []


def test_switched_off_reads_nothing_queues_nothing_and_sends_nothing(tmp_path):
    _, client, tok, conn, folder, popen = world(tmp_path, mode="off")
    apply_own_price(client, tok, conn)
    assert queue(conn) == {} and popen.started == []
    fake = Avito()
    assert run(conn, folder, fake)[0] == {} and fake.calls == []


def test_the_check_mode_lists_what_would_go_and_sends_nothing(tmp_path):
    _, client, tok, conn, folder, popen = world(tmp_path, mode="dry")
    apply_own_price(client, tok, conn)
    assert queue(conn)["1"][1] == 5500 and queue(conn)["1"][3] == "dry"
    assert popen.started == []
    fake = Avito()
    run(conn, folder, fake)
    assert fake.calls == []
    page = client.get("/connections/avito/1").get_data(as_text=True)
    assert "ушло бы (проверка) 1" in page and "→ 5500 ₽ — отправили бы (проверка)" in page


def test_only_ads_with_one_live_avito_ad_that_the_feed_had_before_are_queued(tmp_path):
    ads = {"2": 102, "3": 103}                                            # «1» has no Avito ad yet
    _, client, tok, conn, folder, _ = world(tmp_path, ads=ads)
    conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, status, seen_at) VALUES (1, 203, '3', '', 'active', ?)", (T,))
    for goods_id in ("1", "2", "3"):
        staging.set_own_price(conn, goods_id, [1], {"price": 5600}, T)
    staging.set_override(conn, "6", "on", T)                              # «6» comes back: a new ad, never through the price method
    staging.set_own_price(conn, "6", [1], {"price": 5600}, T)
    post(client, "/draft/%d/confirm" % staging.summary(conn)["draft_id"], tok)
    assert "6" in live_prices(folder)
    assert set(queue(conn)) == {"2"}                                      # «3» has two live ads on Avito: which one is unknown


def test_a_feed_the_gate_stopped_is_not_published_and_queues_nothing(tmp_path, monkeypatch):
    from tires_sync import cycle
    _, client, tok, conn, folder, popen = world(tmp_path)
    before = live_prices(folder)
    monkeypatch.setattr(cycle, "gate", lambda *a, **k: ["стоп"])
    monkeypatch.setattr(cycle.feed_check, "critical", lambda findings: findings)
    monkeypatch.setattr(cycle.feed_check, "summarize", lambda findings: "критическая находка")
    apply_own_price(client, tok, conn)
    assert live_prices(folder) == before and queue(conn) == {} and popen.started == []


# ---- the sender --------------------------------------------------------------------------------------------------------------------------

def test_a_429_waits_and_goes_again_a_lasting_one_stops_the_account_until_the_next_run(tmp_path):
    _, client, tok, conn, folder, _ = world(tmp_path)
    for goods_id in ("1", "2"):
        staging.set_own_price(conn, goods_id, [1], {"price": 5600}, T)
    post(client, "/draft/%d/confirm" % staging.summary(conn)["draft_id"], tok)
    assert set(queue(conn)) == {"1", "2"}
    sleeps = []
    fake = Avito(answers=[(429, {"retry-after": "3"}, b"{}")])
    done, _ = run(conn, folder, fake, sleeps)
    assert done[1]["sent"] == 2 and 3.0 in sleeps and fake.prices == {101: 5600, 102: 5600}
    staging.set_own_price(conn, "1", [1], {"price": 5700}, T)
    staging.set_own_price(conn, "2", [1], {"price": 5700}, T)
    post(client, "/draft/%d/confirm" % staging.summary(conn)["draft_id"], tok)
    fake = Avito(answers=[(429, {}, b"{}")] * 10)
    done, _ = run(conn, folder, fake)
    assert done[1] == {"sent": 0, "failed": 0, "not_sent": 2, "skipped": 0}
    assert len(fake.price_calls()) == 4                                     # one ad: the first try and 3 repeats, then the account stops
    assert queue(conn)["2"][3:] == ("not_sent", "остановлено: 429 HTTP 429")
    fake = Avito()
    run(conn, folder, fake)                                                 # the next run (the hourly cycle) sends what waits
    assert fake.prices == {101: 5700, 102: 5700}


def test_refused_keys_stop_before_any_price_and_an_auth_error_stops_the_rest(tmp_path):
    _, client, tok, conn, folder, _ = world(tmp_path)
    for goods_id in ("1", "2"):
        staging.set_own_price(conn, goods_id, [1], {"price": 5600}, T)
    post(client, "/draft/%d/confirm" % staging.summary(conn)["draft_id"], tok)
    fake = Avito(token=401)
    done, _ = run(conn, folder, fake)
    assert fake.price_calls() == [] and done[1]["not_sent"] == 2
    assert queue(conn)["1"][4].startswith("токен Авито")
    fake = Avito(answers=[(403, {}, json.dumps({"error": {"code": 403, "message": "forbidden"}}).encode())])
    done, _ = run(conn, folder, fake)
    assert len(fake.price_calls()) == 1 and done[1] == {"sent": 0, "failed": 0, "not_sent": 2, "skipped": 0}


def test_an_ad_avito_refuses_is_an_error_the_others_go_and_the_card_lists_it(tmp_path):
    _, client, tok, conn, folder, _ = world(tmp_path)
    for goods_id in ("1", "2"):
        staging.set_own_price(conn, goods_id, [1], {"price": 5600}, T)
    post(client, "/draft/%d/confirm" % staging.summary(conn)["draft_id"], tok)
    fake = Avito(answers=[(400, {}, json.dumps({"error": {"code": 400, "message": "item not found"}}).encode())])
    done, _ = run(conn, folder, fake)
    assert done[1] == {"sent": 1, "failed": 1, "not_sent": 0, "skipped": 0} and fake.prices == {102: 5600}
    page = client.get("/connections/avito/1").get_data(as_text=True)
    assert "Цены на Авито (" in page and "отправлено 1, ошибок 1" in page and "ошибка: 400 item not found" in page


def test_the_price_sent_is_the_published_feeds_at_the_moment_and_a_gone_ad_is_not_sent(tmp_path):
    _, client, tok, conn, folder, _ = world(tmp_path)
    for goods_id in ("1", "2"):
        staging.set_own_price(conn, goods_id, [1], {"price": 5600}, T)
    post(client, "/draft/%d/confirm" % staging.summary(conn)["draft_id"], tok)
    path = folder / "avito_feed.xml"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(<Ad>(?:(?!</Ad>).)*?<Id>1</Id>(?:(?!</Ad>).)*?<Price>)5600(</Price>)", r"\g<1>5900\2", text, flags=re.S)
    text = re.sub(r"<Ad>(?:(?!</Ad>).)*?<Id>2</Id>.*?</Ad>", "", text, flags=re.S)
    path.write_text(text, encoding="utf-8")                               # a cycle published again since: «1» at 5900, «2» gone
    fake = Avito()
    done, _ = run(conn, folder, fake)
    assert fake.prices == {101: 5900} and done[1]["skipped"] == 1
    assert queue(conn)["2"][3] == "skipped"


def test_switched_off_after_queueing_sends_nothing(tmp_path):
    _, client, tok, conn, folder, _ = world(tmp_path)
    apply_own_price(client, tok, conn)
    price_push.save_mode(conn, 1, "off", T, "owner")
    fake = Avito()
    run(conn, folder, fake)
    assert fake.calls == [] and queue(conn)["1"][3] == "skipped"


def test_one_sender_at_a_time(tmp_path):
    _, client, tok, conn, folder, _ = world(tmp_path)
    apply_own_price(client, tok, conn)
    held = price_push._try_lock(folder.parent)
    try:
        fake = Avito()
        done, out = run(conn, folder, fake)
        assert done == {} and fake.calls == [] and "другой процесс" in out
    finally:
        held.close()


def test_the_switch_is_the_owners_and_is_off_by_default(tmp_path):
    from tires_sync.panel import common
    assert common.NEEDS["connections_account_price_push"] == "publish"
    _, client, tok, conn, _, _ = world(tmp_path, mode="off")
    page = client.get("/connections/avito/1").get_data(as_text=True)
    assert "Цены сразу через API" in page and 'name="mode" value="off" checked' in page
    assert post(client, "/connections/accounts/1/price-push", tok, mode="on").status_code == 302
    assert price_push.mode(conn, 1) == "on"
    post(client, "/connections/accounts/1/price-push", tok, mode="всегда")
    assert price_push.mode(conn, 1) == "on"


def test_the_flash_of_a_publication_keeps_the_price_line_apart():
    lines = ["Опубликовано: Основной, объявлений 5 (было 5)", "Цены на Авито: отправляются сразу, объявлений 1."]
    assert publish_summary(lines) == "Фид обновлён для Авито: Основной, объявлений 5 (было 5). Цены на Авито: отправляются сразу, объявлений 1."


# ---- the command and the hourly cycle -----------------------------------------------------------------------------------------------------

def test_the_command_shows_the_queue_and_sends_it(tmp_path, monkeypatch, capsys):
    from tires_sync import cli
    app, client, tok, conn, folder, _ = world(tmp_path)
    apply_own_price(client, tok, conn)
    fake = Avito()
    monkeypatch.setattr(cli, "load_box", lambda path: Box())
    monkeypatch.setattr(cli, "_http", lambda: fake)
    assert cli.main(["price-push", "--db", app.config["DB_PATH"], "--feed-root", str(folder.parent), "--show"]) == 0
    shown = capsys.readouterr().out
    assert "режим «включено»" in shown and "1 (Авито 101): 4850 → 5500, в очереди" in shown and fake.calls == []
    assert cli.main(["price-push", "--db", app.config["DB_PATH"], "--feed-root", str(folder.parent)]) == 0
    assert fake.prices == {101: 5500}


def test_the_hourly_cycle_sends_what_a_stopped_sender_left(tmp_path, monkeypatch):
    from tires_sync import cli
    app, client, tok, conn, folder, _ = world(tmp_path)
    apply_own_price(client, tok, conn)                                    # the sender of the panel never ran (killed by a restart)
    fake = Avito()
    monkeypatch.setattr(cli, "load_box", lambda path: Box())
    monkeypatch.setattr(cli, "_http", lambda: fake)
    for step in ("_collect_avito_reports", "_heal_accounts", "_check_waiting", "_send_reminders", "_alert"):
        monkeypatch.setattr(cli, step, lambda *a, **k: None)
    monkeypatch.setattr(cli.avito_stock, "after_cycle", lambda *a, **k: None)
    cli.main(["run-cycle", "--publish", "--db", app.config["DB_PATH"], "--unload", app.config["UNLOAD_PATH"],
              "--reference", app.config["REFERENCE_PATH"], "--feed-root", str(folder.parent)])
    assert fake.prices == {101: 5500} and queue(conn)["1"][3] == "sent"


def test_a_new_apply_while_sending_is_never_overwritten_by_the_older_price(tmp_path):
    """Review 2026-11-03: the sender read the feed once and marked rows queued again meanwhile as sent with the old price."""
    _, client, tok, conn, folder, _ = world(tmp_path)
    for goods_id in ("1", "2"):
        staging.set_own_price(conn, goods_id, [1], {"price": 5600}, T)
    post(client, "/draft/%d/confirm" % staging.summary(conn)["draft_id"], tok)
    path = folder / "avito_feed.xml"

    def publish_again(ad_id, price):                                      # another «Применить»: the feed and the queue change
        text = re.sub(r"(<Ad>(?:(?!</Ad>).)*?<Id>%s</Id>(?:(?!</Ad>).)*?<Price>)\d+(</Price>)" % ad_id, r"\g<1>%d\2" % price,
                      path.read_text(encoding="utf-8"), flags=re.S)
        path.write_text(text, encoding="utf-8")
        conn.execute("UPDATE avito_price SET price = ?, status = 'queued', queued_at = ? WHERE ad_id = ?", (price, "2026-11-03T12:00:%02d" % (price % 60), ad_id))
        conn.commit()

    class During(Avito):
        def __call__(self, method, url, headers, body):
            answer = super().__call__(method, url, headers, body)
            if url.endswith("/101/update_price") and len(self.price_calls()) == 1:
                publish_again("2", 5800)                                  # «2» not sent yet: its new price goes
                publish_again("1", 5900)                                  # «1» sent a moment ago: it goes again
            return answer

    fake = During()
    done, _ = run(conn, folder, fake)
    assert fake.prices == {101: 5900, 102: 5800}
    assert [c[1] for c in fake.price_calls()] == ["/core/v1/items/101/update_price", "/core/v1/items/102/update_price",
                                                  "/core/v1/items/101/update_price"]
    assert queue(conn)["1"][1:4] == (5900, 4850, "sent") and queue(conn)["2"][1] == 5800 and queue(conn)["2"][3] == "sent"


def test_a_token_service_down_marks_the_rows_and_says_why(tmp_path):
    _, client, tok, conn, folder, _ = world(tmp_path)
    apply_own_price(client, tok, conn)
    fake = Avito(token=503)
    done, _ = run(conn, folder, fake)
    assert fake.price_calls() == [] and done[1]["not_sent"] == 1 and queue(conn)["1"][4].startswith("токен Авито")


def test_the_card_says_when_no_avito_ad_numbers_were_read(tmp_path):
    _, client, tok, conn, _, _ = world(tmp_path, ads={})
    assert "Номера объявлений на Авито ещё не прочитаны" in client.get("/connections/avito/1").get_data(as_text=True)
