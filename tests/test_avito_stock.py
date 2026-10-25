"""The number in stock of our ads on Avito (stock-management API): off by default, the list, the sets of four, only what changed, the
journal, the batches, 429 / 5xx without a blind repeat, --restore, the hook after a cycle and the command (a fake transport, no network)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from helpers import catalog_xml, commodity_xml, settings_dict
from http_fakes import ScriptedHttp
from tires_sync import avito_stock, cli, connections, store
from tires_sync.avito import AvitoClient, AvitoError, WriteDisabled
from tires_sync.cli import main
from tires_sync.secretbox import create_key_file, load_box
from tires_sync.settings_store import init_settings

NOW = datetime(2026, 10, 24, 12, 0, 0)
NOW_ISO = "2026-10-24T12:00:00"
TOKEN = (200, {"access_token": "t", "expires_in": 3600})


class Avito:
    """Avito as the stock methods see it: `stock` is what `info` shows per item_id (an id it does not know fails the whole request with
    400, as the real one does); `put` is a queue of answers for the next PUTs (None: success for every ad)."""

    def __init__(self, stock=None, ids=None, refuse=(), put=(), dead=(), token=200, down=()):
        self.stock = dict(stock or {})
        self.down = set(down)                 # an id whose reading Avito answers 503 (the whole request), every time
        self.dead = set(dead)                 # an id PUT refuses with 400 for the whole batch
        self.token = token
        self.ids = dict(ids or {})            # our Id -> item_id, for /autoload/v2/items/avito_ids
        self.refuse = dict.fromkeys(refuse, ["Нельзя указать количество"]) if not isinstance(refuse, dict) else refuse
        self.put = list(put)
        self.calls = []
        self.puts = []

    def __call__(self, method, url, headers, body):
        path = urlparse(url).path
        self.calls.append((method, path))
        if path == "/token/":
            if self.token != 200:
                return self.token, {}, json.dumps({"error": "invalid_client"}).encode()
            return 200, {}, json.dumps({"access_token": "t", "expires_in": 3600}).encode()
        if path == "/stock-management/1/info":
            ids = json.loads(body)["item_ids"]
            assert json.loads(body)["strong_consistency"] is True
            if any(i in self.down for i in ids) or "*" in self.down:
                return 503, {}, json.dumps({"error": {"code": 503, "message": "down"}}).encode()
            if any(i not in self.stock for i in ids):
                return 400, {}, json.dumps({"error": {"code": 400, "message": "item not found"}}).encode()
            return 200, {}, json.dumps({"stocks": [dict(item_id=i, quantity=self.stock[i], is_multiple=False, is_unlimited=False,
                                                         is_out_of_stock=False) for i in ids]}).encode()
        if path == "/stock-management/1/stocks":
            assert method == "PUT"
            stocks = json.loads(body)["stocks"]
            self.puts.append([(s["item_id"], s["quantity"]) for s in stocks])
            answer = self.put.pop(0) if self.put else None
            if answer is not None:
                return answer
            if any(s["item_id"] in self.dead for s in stocks):
                return 400, {}, json.dumps({"error": {"code": 400, "message": "item not found"}}).encode()
            out = []
            for s in stocks:
                if s["item_id"] in self.refuse:
                    out.append({"item_id": s["item_id"], "success": False, "errors": self.refuse[s["item_id"]]})
                else:
                    self.stock[s["item_id"]] = s["quantity"]
                    out.append({"item_id": s["item_id"], "success": True, "errors": []})
            return 200, {}, json.dumps({"stocks": out}).encode()
        if path == "/autoload/v2/items/avito_ids":
            wanted = parse_qs(urlparse(url).query)["query"][0].split(",")
            return 200, {}, json.dumps({"items": [{"ad_id": w, "avito_id": self.ids.get(w)} for w in wanted]}).encode()
        raise AssertionError("unexpected request %s %s" % (method, url))

    def writes(self):
        return [c for c in self.calls if c[0] == "PUT"]


def client(fake, write=True, sleeps=None):
    return AvitoClient("id", "secret", http=fake, now=lambda: 0.0, sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
                       allow_write=write)


@pytest.fixture(autouse=True)
def no_real_pause(monkeypatch):
    monkeypatch.setattr(avito_stock, "PAUSE", 0.0)                         # the pauses are counted, not waited


@pytest.fixture
def world(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    init_settings(conn, settings_dict(), NOW_ISO)
    account_id = connections.create_account(conn, "Основной", NOW_ISO, "owner")
    connections.set_live(conn, account_id, True, NOW_ISO, "owner")
    account = connections.get_account(conn, account_id)
    root = tmp_path / "feed"
    (root / account["feed_token"]).mkdir(parents=True)
    return SimpleNamespace(conn=conn, account=account, root=root, tmp=tmp_path)


def feed(w, *ids):
    (w.root / w.account["feed_token"] / "avito_feed.xml").write_text(
        '<Ads formatVersion="3">%s</Ads>' % "".join("<Ad><Id>%s</Id></Ad>" % i for i in ids), encoding="utf-8")


def ads(w, **items):
    """avito_ad rows: our Id -> item_id (as the daily statistics map them)."""
    for ad_id, avito_id in items.items():
        w.conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, status, seen_at) VALUES (?,?,?,?,?,?)",
                       (w.account["id"], avito_id, ad_id.lstrip("_"), "", "active", NOW_ISO))


def unload(stock=None, issues=()):
    return SimpleNamespace(offers={code: SimpleNamespace(stock=n) for code, n in (stock or {}).items()},
                           issues={code: ["bad"] for code in issues})


def cfg(mode="all", codes=(), cap=None):
    return {"mode": mode, "codes": list(codes), "cap": cap}


def lines_by_id(p):
    return {line.ad_id: line for line in p.lines}


# ---- the client --------------------------------------------------------------------------------------------------------------------------

def test_stocks_info_is_a_read_that_goes_again_after_a_5xx():
    http = ScriptedHttp(TOKEN, (502, {}), (200, {"stocks": [{"item_id": 7, "quantity": 3, "is_multiple": True, "is_unlimited": False,
                                                              "is_out_of_stock": False}]}))
    got = AvitoClient("id", "secret", http=http, now=lambda: 0.0, sleep=lambda s: None).stocks_info([7])
    assert got[0]["quantity"] == 3
    assert [c[0] for c in http.calls] == ["POST", "POST", "POST"]
    assert json.loads(http.calls[-1][3]) == {"item_ids": [7], "strong_consistency": True}
    with pytest.raises(ValueError):
        AvitoClient("id", "secret", http=http).stocks_info(list(range(11)))


def test_set_stocks_needs_a_writing_client_and_is_made_once_whatever_the_answer():
    http = ScriptedHttp(TOKEN)
    with pytest.raises(WriteDisabled):
        AvitoClient("id", "secret", http=http).set_stocks([(1, 2)])
    assert http.calls == []
    http = ScriptedHttp(TOKEN, (500, {}))
    with pytest.raises(AvitoError):
        AvitoClient("id", "secret", http=http, now=lambda: 0.0, sleep=lambda s: None, allow_write=True).set_stocks([(1, 2)])
    assert [c[0] for c in http.calls] == ["POST", "PUT"]                     # the token, then one PUT: never repeated
    assert json.loads(http.calls[1][3]) == {"stocks": [{"item_id": 1, "quantity": 2}]}
    writer = AvitoClient("id", "secret", http=ScriptedHttp(TOKEN), allow_write=True)
    with pytest.raises(ValueError):
        writer.set_stocks([(i, 1) for i in range(201)])
    with pytest.raises(ValueError):
        writer.set_stocks([(1, 1000000)])
    with pytest.raises(ValueError):
        writer.set_stocks([(1, -1)])


# ---- the switch ----------------------------------------------------------------------------------------------------------------------------

def test_the_switch_is_off_by_default_and_checked_when_saved(world):
    assert avito_stock.config(world.conn, world.account["id"]) == {"mode": "off", "codes": [], "cap": None}
    with pytest.raises(ValueError):
        avito_stock.save_config(world.conn, world.account["id"], "list", "", "", NOW_ISO, "owner")          # a list without codes
    with pytest.raises(ValueError):
        avito_stock.save_config(world.conn, world.account["id"], "all", "", "0", NOW_ISO, "owner")
    with pytest.raises(ValueError):
        avito_stock.save_config(world.conn, world.account["id"], "all", "12 <b>", "", NOW_ISO, "owner")
    with pytest.raises(ValueError):
        avito_stock.save_config(world.conn, world.account["id"], "on", "", "", NOW_ISO, "owner")
    saved = avito_stock.save_config(world.conn, world.account["id"], "list", "101, 102\n101", "20", NOW_ISO, "owner")
    assert saved == {"mode": "list", "codes": ["101", "102"], "cap": 20}
    assert avito_stock.config(world.conn, world.account["id"]) == saved
    assert world.conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'avito_stock_config'").fetchone()[0] == 1


# ---- what each ad should show -------------------------------------------------------------------------------------------------------------

def test_the_supplier_stock_goes_as_it_is_a_set_of_four_in_whole_sets_and_the_ceiling_holds(world):
    feed(world, "101", "102", "102-x4", "103-x4_r20261001", "104", "105")
    ads(world, **{"101": 1, "102": 2, "102-x4": 3, "103-x4_r20261001": 4, "104": 5, "105": 6})
    p = avito_stock.plan(world.conn, world.account, unload({"101": 7, "102": 11, "103": 3, "104": 120, "105": 1500000}), world.root, cfg())
    got = {line.ad_id: (line.quantity, line.avito_id) for line in p.lines}
    assert got == {"101": (7, 1), "102": (11, 2), "102-x4": (2, 3), "103-x4_r20261001": (0, 4), "104": (120, 5), "105": (999999, 6)}
    capped = avito_stock.plan(world.conn, world.account, unload({"101": 7, "102": 11, "103": 3, "104": 120}), world.root, cfg(cap=20))
    assert lines_by_id(capped)["104"].quantity == 20 and lines_by_id(capped)["101"].quantity == 7


def test_an_unknown_stock_or_an_own_point_sends_nothing(world):
    feed(world, "101", "102", "103")
    ads(world, **{"101": 1, "102": 2, "103": 3})
    world.conn.execute("INSERT INTO published_state (account_id, goods_id, price, base_price, point, location, snapshot) "
                       "VALUES (?, '103', 5000, 4000, 'p', 'own', '')", (world.account["id"],))
    p = lines_by_id(avito_stock.plan(world.conn, world.account, unload({"102": 5, "103": 9}, issues=["102"]), world.root, cfg()))
    assert (p["101"].quantity, p["101"].skip) == (None, avito_stock.SKIP_UNKNOWN)          # not in the unload
    assert (p["102"].quantity, p["102"].skip) == (None, avito_stock.SKIP_UNKNOWN)          # a row with an error
    assert (p["103"].quantity, p["103"].skip) == (None, avito_stock.SKIP_OWN)


def test_the_list_mode_takes_only_the_listed_codes(world):
    feed(world, "101", "102", "102-x4")
    ads(world, **{"101": 1, "102": 2, "102-x4": 3})
    p = lines_by_id(avito_stock.plan(world.conn, world.account, unload({"101": 5, "102": 8}), world.root, cfg("list", ["102"])))
    assert p["101"].skip == avito_stock.SKIP_NOT_LISTED and p["101"].quantity is None
    assert (p["102"].quantity, p["102-x4"].quantity) == (8, 2)


def test_an_ad_without_its_avito_id_is_looked_up_or_skipped(world):
    feed(world, "101", "102", "103")
    ads(world, **{"103": 3, "_103": 33})                                     # two live ads under one Id: which one is ours is unknown
    fake = Avito(ids={"101": 11})
    p = lines_by_id(avito_stock.plan(world.conn, world.account, unload({"101": 5, "102": 6, "103": 7}), world.root, cfg(), client=client(fake, write=False)))
    assert (p["101"].avito_id, p["101"].quantity) == (11, 5)
    assert p["102"].skip == avito_stock.SKIP_NO_ITEM and p["102"].quantity is None
    assert p["103"].skip == avito_stock.SKIP_MANY_ITEMS and p["103"].quantity is None
    assert fake.calls[-1] == ("GET", "/autoload/v2/items/avito_ids")
    offline = lines_by_id(avito_stock.plan(world.conn, world.account, unload({"101": 5}), world.root, cfg()))
    assert offline["101"].skip == avito_stock.SKIP_NO_ITEM


def test_no_feed_or_a_mirror_account_gives_nothing_at_all(world):
    assert avito_stock.plan(world.conn, world.account, unload({"101": 5}), world.root, cfg()).note
    feed(world, "101")
    mirror = dict(world.account, feed_mode="mirror")
    p = avito_stock.plan(world.conn, mirror, unload({"101": 5}), world.root, cfg())
    assert p.note and p.lines == []


# ---- sending -------------------------------------------------------------------------------------------------------------------------------

def test_the_first_send_keeps_what_avito_showed_then_only_changes_go(world):
    feed(world, "101", "102")
    ads(world, **{"101": 1, "102": 2})
    fake = Avito(stock={1: 1, 2: 1})
    sleeps = []
    c = client(fake, sleeps=sleeps)
    p = avito_stock.plan(world.conn, world.account, unload({"101": 7, "102": 4}), world.root, cfg(), client=c, now=NOW)
    assert avito_stock.push(world.conn, c, world.account, p.lines, NOW, sleep=sleeps.append) == {"sent": 2, "failed": 0, "unknown": 0, "not_sent": 0}
    assert fake.puts == [[(1, 7), (2, 4)]]
    assert world.conn.execute("SELECT avito_id, quantity, status, before_quantity FROM avito_stock ORDER BY avito_id").fetchall() == [
        (1, 7, "sent", 1), (2, 4, "sent", 1)]
    assert sleeps == [avito_stock.PAUSE]                                       # one info request, a pause, one PUT
    # the next pass: 101 dropped to 5, 102 unchanged; no second reading of what Avito showed
    fake.calls.clear()
    p = avito_stock.plan(world.conn, world.account, unload({"101": 5, "102": 4}), world.root, cfg(), client=c, now=NOW)
    assert lines_by_id(p)["102"].skip == avito_stock.SKIP_SAME and lines_by_id(p)["101"].last == 7
    avito_stock.push(world.conn, c, world.account, p.lines, NOW, sleep=sleeps.append)
    assert fake.puts[-1] == [(1, 5)]
    assert ("POST", "/stock-management/1/info") not in fake.calls
    assert world.conn.execute("SELECT quantity, before_quantity FROM avito_stock WHERE avito_id = 1").fetchone() == (5, 1)
    assert world.conn.execute("SELECT detail FROM audit WHERE action = 'avito_stock_push' ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_an_ad_that_left_the_feed_gets_0_once(world):
    feed(world, "101", "102")
    ads(world, **{"101": 1, "102": 2})
    fake = Avito(stock={1: 1, 2: 1})
    c = client(fake)
    avito_stock.push(world.conn, c, world.account, avito_stock.plan(world.conn, world.account, unload({"101": 7, "102": 6}), world.root, cfg(), now=NOW).lines, NOW)
    feed(world, "101")                                                         # 102 went below the threshold and left the feed
    p = avito_stock.plan(world.conn, world.account, unload({"101": 7, "102": 2}), world.root, cfg(), now=NOW)
    gone = [line for line in p.lines if line.gone]
    assert [(line.ad_id, line.avito_id, line.quantity) for line in gone] == [("102", 2, 0)]
    avito_stock.push(world.conn, c, world.account, p.lines, NOW)
    assert fake.puts[-1] == [(2, 0)]
    again = avito_stock.plan(world.conn, world.account, unload({"101": 7}), world.root, cfg(), now=NOW)
    assert not [line for line in again.lines if line.gone]                     # already 0 on Avito: not sent again


def test_a_position_still_in_the_feed_under_another_id_is_not_zeroed(world):
    feed(world, "101")
    ads(world, **{"101": 1})
    for avito_id, ad_id, code, quantity, status in [(9, "k0old000000a", "101", 5, "sent"),       # the old Id of the same position
                                                      (1, "k0gone00000b", "300", 5, "sent"),       # its item is the one of a live line
                                                      (7, "777", "777", 0, "failed")]:            # Avito refused 0 (a deleted ad)
        world.conn.execute("INSERT INTO avito_stock (account_id, avito_id, ad_id, code, quantity, status, at) VALUES (?,?,?,?,?,?,?)",
                           (world.account["id"], avito_id, ad_id, code, quantity, status, "2026-10-01T00:00:00"))
    p = avito_stock.plan(world.conn, world.account, unload({"101": 5}), world.root, cfg(), now=NOW)
    assert [line for line in p.lines if line.gone] == []


def test_batches_of_200_with_a_pause_between_requests(world):
    ids = [str(1000 + i) for i in range(201)]
    feed(world, *ids)
    ads(world, **{code: int(code) for code in ids})
    fake = Avito(stock={int(code): 1 for code in ids})
    sleeps = []
    c = client(fake)
    p = avito_stock.plan(world.conn, world.account, unload({code: 5 for code in ids}), world.root, cfg(), now=NOW)
    counts = avito_stock.push(world.conn, c, world.account, p.lines, NOW, sleep=sleeps.append)
    assert counts["sent"] == 201
    assert [len(batch) for batch in fake.puts] == [200, 1]
    info_calls = sum(1 for call in fake.calls if call[1] == "/stock-management/1/info")
    assert info_calls == 21                                                    # 10 ads per reading
    assert len(sleeps) == info_calls + 2 - 1                                   # a pause before every request but the first


def test_429_stops_the_pass_and_leaves_the_rest_not_sent(world):
    ids = [str(1000 + i) for i in range(250)]
    feed(world, *ids)
    ads(world, **{code: int(code) for code in ids})
    fake = Avito(stock={int(code): 1 for code in ids}, put=[(429, {"retry-after": "5"}, b'{"error": {"code": 429, "message": "too many"}}')])
    p = avito_stock.plan(world.conn, world.account, unload({code: 5 for code in ids}), world.root, cfg(), now=NOW)
    counts = avito_stock.push(world.conn, client(fake), world.account, p.lines, NOW)
    assert counts == {"sent": 0, "failed": 0, "unknown": 0, "not_sent": 250}
    assert len(fake.writes()) == 1                                             # neither the 429 nor the next batch was tried again
    assert world.conn.execute("SELECT COUNT(*) FROM avito_stock WHERE status = 'not_sent'").fetchone()[0] == 250
    again = avito_stock.plan(world.conn, world.account, unload({code: 5 for code in ids}), world.root, cfg(), now=NOW)
    assert sum(1 for line in again.lines if line.quantity is not None) == 250  # the next pass sends them


def test_a_5xx_marks_the_batch_unknown_without_a_repeat_and_the_next_pass_sends_it_again(world):
    ids = [str(1000 + i) for i in range(250)]
    feed(world, *ids)
    ads(world, **{code: int(code) for code in ids})
    fake = Avito(stock={int(code): 1 for code in ids}, put=[(500, {}, b"")])
    p = avito_stock.plan(world.conn, world.account, unload({code: 5 for code in ids}), world.root, cfg(), now=NOW)
    counts = avito_stock.push(world.conn, client(fake), world.account, p.lines, NOW)
    assert counts == {"sent": 0, "failed": 0, "unknown": 200, "not_sent": 50}
    assert len(fake.writes()) == 1
    again = avito_stock.plan(world.conn, world.account, unload({code: 5 for code in ids}), world.root, cfg(), now=NOW)
    assert sum(1 for line in again.lines if line.quantity is not None) == 250


def test_keys_refused_on_the_write_leave_it_not_sent_and_stop_the_pass(world):
    ids = [str(1000 + i) for i in range(250)]
    feed(world, *ids)
    ads(world, **{code: int(code) for code in ids})
    fake = Avito(stock={int(code): 1 for code in ids}, put=[(403, {}, b'{"error": {"code": 403, "message": "forbidden"}}')])
    p = avito_stock.plan(world.conn, world.account, unload({code: 5 for code in ids}), world.root, cfg(), now=NOW)
    assert avito_stock.push(world.conn, client(fake), world.account, p.lines, NOW) == {"sent": 0, "failed": 0, "unknown": 0, "not_sent": 250}
    assert len(fake.writes()) == 1


def test_a_batch_avito_refuses_as_a_whole_goes_again_one_ad_at_a_time(world):
    feed(world, "101", "102", "103")
    ads(world, **{"101": 1, "102": 2, "103": 3})
    fake = Avito(stock={1: 1, 2: 1, 3: 1}, dead=[2])
    p = avito_stock.plan(world.conn, world.account, unload({"101": 5, "102": 6, "103": 7}), world.root, cfg(), now=NOW)
    assert avito_stock.push(world.conn, client(fake), world.account, p.lines, NOW) == {"sent": 2, "failed": 1, "unknown": 0, "not_sent": 0}
    assert fake.puts == [[(1, 5), (2, 6), (3, 7)], [(1, 5)], [(2, 6)], [(3, 7)]]
    assert world.conn.execute("SELECT status FROM avito_stock WHERE avito_id = 2").fetchone()[0] == "failed"


def test_refused_keys_cost_one_token_request_and_leave_everything_not_sent(world):
    feed(world, *[str(1000 + i) for i in range(25)])
    ads(world, **{str(1000 + i): 1000 + i for i in range(25)})
    fake = Avito(stock={1000 + i: 1 for i in range(25)}, token=400)
    p = avito_stock.plan(world.conn, world.account, unload({str(1000 + i): 5 for i in range(25)}), world.root, cfg(), now=NOW)
    assert avito_stock.push(world.conn, client(fake), world.account, p.lines, NOW) == {"sent": 0, "failed": 0, "unknown": 0, "not_sent": 25}
    assert fake.calls == [("POST", "/token/")]
    # the same on the write, when what Avito showed is already kept
    world.conn.execute("UPDATE avito_stock SET before_at = ?, before_quantity = 1", (NOW_ISO,))
    fake = Avito(stock={1000 + i: 1 for i in range(25)}, token=400)
    p = avito_stock.plan(world.conn, world.account, unload({str(1000 + i): 5 for i in range(25)}), world.root, cfg(), now=NOW)
    assert avito_stock.push(world.conn, client(fake), world.account, p.lines, NOW)["not_sent"] == 25
    assert fake.calls == [("POST", "/token/")]
    assert world.conn.execute("SELECT COUNT(*) FROM avito_stock WHERE status = 'failed'").fetchone()[0] == 0


def test_a_refused_ad_is_failed_with_avitos_words_and_offered_again_after_a_day(world):
    feed(world, "101", "102")
    ads(world, **{"101": 1, "102": 2})
    fake = Avito(stock={1: 1, 2: 1}, refuse=[2])
    c = client(fake)
    p = avito_stock.plan(world.conn, world.account, unload({"101": 7, "102": 6}), world.root, cfg(), now=NOW)
    assert avito_stock.push(world.conn, c, world.account, p.lines, NOW)["failed"] == 1
    assert world.conn.execute("SELECT status, error FROM avito_stock WHERE avito_id = 2").fetchone() == ("failed", "Нельзя указать количество")
    soon = lines_by_id(avito_stock.plan(world.conn, world.account, unload({"101": 7, "102": 6}), world.root, cfg(), now=NOW + timedelta(hours=2)))
    assert soon["102"].skip == avito_stock.SKIP_REFUSED
    later = lines_by_id(avito_stock.plan(world.conn, world.account, unload({"101": 7, "102": 6}), world.root, cfg(), now=NOW + timedelta(days=1, hours=1)))
    assert later["102"].quantity == 6
    changed = lines_by_id(avito_stock.plan(world.conn, world.account, unload({"101": 7, "102": 5}), world.root, cfg(), now=NOW + timedelta(hours=2)))
    assert changed["102"].quantity == 5                                        # another number: offered at once


def test_an_ad_avito_does_not_know_in_the_reading_is_not_sent_the_others_are(world):
    feed(world, "101", "102")
    ads(world, **{"101": 1, "102": 2})
    fake = Avito(stock={1: 1})                                                 # 2 is unknown: the batch of two fails, then one by one
    p = avito_stock.plan(world.conn, world.account, unload({"101": 7, "102": 6}), world.root, cfg(), now=NOW)
    counts = avito_stock.push(world.conn, client(fake), world.account, p.lines, NOW)
    assert counts["sent"] == 1 and counts["failed"] == 1
    assert fake.puts == [[(1, 7)]]
    assert world.conn.execute("SELECT status FROM avito_stock WHERE avito_id = 2").fetchone()[0] == "failed"


def test_a_reading_that_fails_on_avitos_side_sends_nothing(world):
    feed(world, "101")
    ads(world, **{"101": 1})
    http = ScriptedHttp(TOKEN, (503, {}), (503, {}), (503, {}))
    c = AvitoClient("id", "secret", http=http, now=lambda: 0.0, sleep=lambda s: None, allow_write=True)
    p = avito_stock.plan(world.conn, world.account, unload({"101": 7}), world.root, cfg(), now=NOW)
    assert avito_stock.push(world.conn, c, world.account, p.lines, NOW)["not_sent"] == 1
    assert not [call for call in http.calls if call[0] == "PUT"]


def test_a_reading_that_fails_on_avitos_side_for_one_ad_holds_only_that_ad(world):
    """tails2 Ф9: one ad whose reading Avito keeps answering 5xx: the batch is read one ad at a time; that ad is «not sent» (offered again
    next pass), the others go."""
    feed(world, "101", "102", "103")
    ads(world, **{"101": 1, "102": 2, "103": 3})
    fake = Avito(stock={1: 1, 2: 1, 3: 1}, down=[2])
    p = avito_stock.plan(world.conn, world.account, unload({"101": 7, "102": 6, "103": 5}), world.root, cfg(), now=NOW)
    counts = avito_stock.push(world.conn, client(fake), world.account, p.lines, NOW)
    assert counts == {"sent": 2, "failed": 0, "unknown": 0, "not_sent": 1}
    assert fake.puts == [[(1, 7), (3, 5)]]
    assert world.conn.execute("SELECT status, error FROM avito_stock WHERE avito_id = 2").fetchone()[0] == "not_sent"


def test_avito_down_for_every_reading_stops_the_pass_after_a_few_ads_not_one_request_per_ad(world):
    ids = [str(1000 + i) for i in range(30)]
    feed(world, *ids)
    ads(world, **{code: int(code) for code in ids})
    fake = Avito(stock={int(code): 1 for code in ids}, down=["*"])
    p = avito_stock.plan(world.conn, world.account, unload({code: 5 for code in ids}), world.root, cfg(), now=NOW)
    assert avito_stock.push(world.conn, client(fake), world.account, p.lines, NOW)["not_sent"] == 30
    reads = sum(1 for c in fake.calls if c[1] == "/stock-management/1/info")
    assert reads == 3 * (1 + avito_stock.DOWN_STOP)                              # each request: the client's own two repeats of a 5xx
    assert fake.writes() == []


# ---- restore -------------------------------------------------------------------------------------------------------------------------------

def test_restore_puts_back_what_avito_showed_and_only_when_switched_off(world):
    feed(world, "101", "102")
    ads(world, **{"101": 1, "102": 2})
    fake = Avito(stock={1: 3, 2: 1})
    c = client(fake)
    avito_stock.save_config(world.conn, world.account["id"], "all", "", "", NOW_ISO, "owner")
    avito_stock.push(world.conn, c, world.account, avito_stock.plan(world.conn, world.account, unload({"101": 7, "102": 6}), world.root,
                                                                   avito_stock.config(world.conn, world.account["id"]), now=NOW).lines, NOW)
    with pytest.raises(ValueError):
        avito_stock.restore(world.conn, c, world.account, NOW)
    avito_stock.save_config(world.conn, world.account["id"], "off", "", "", NOW_ISO, "owner")
    assert avito_stock.restore(world.conn, c, world.account, NOW) == {"restored": 2, "failed": 0, "unknown": 0, "not_sent": 0}
    assert fake.puts[-1] == [(1, 3), (2, 1)]
    assert world.conn.execute("SELECT status, quantity FROM avito_stock ORDER BY avito_id").fetchall() == [("restored", 3), ("restored", 1)]
    assert avito_stock.restore_lines(world.conn, world.account["id"]) == []


# ---- after a cycle -------------------------------------------------------------------------------------------------------------------------

def _keys(w):
    key_file = w.tmp / "secret.key"
    create_key_file(key_file)
    box = load_box(key_file)
    connections.set_secret(w.conn, box, "avito", w.account["id"], "client_id", "CLIENTID-12345678", NOW_ISO, "owner")
    connections.set_secret(w.conn, box, "avito", w.account["id"], "client_secret", "SECRETVALUE-XYZ", NOW_ISO, "owner")
    return box, key_file


def result(w, ok=True, tripped=False):
    return SimpleNamespace(ok=ok, guard_tripped=tripped, account_id=w.account["id"])


def test_after_a_cycle_an_account_switched_off_costs_not_a_single_request(world, capsys):
    box, _ = _keys(world)
    feed(world, "101")
    ads(world, **{"101": 1})
    fake = Avito(stock={1: 1})
    avito_stock.after_cycle(world.conn, box, world.root, unload({"101": 7}), [result(world)], http=fake, now=NOW, sleep=lambda s: None)
    assert fake.calls == []


def test_after_a_cycle_the_list_of_one_code_sends_one_number(world, capsys):
    box, _ = _keys(world)
    feed(world, "101", "102")
    ads(world, **{"101": 1, "102": 2})
    avito_stock.save_config(world.conn, world.account["id"], "list", "102", "", NOW_ISO, "owner")
    fake = Avito(stock={1: 1, 2: 1})
    avito_stock.after_cycle(world.conn, box, world.root, unload({"101": 7, "102": 6}), [result(world)], http=fake, now=NOW, sleep=lambda s: None)
    assert fake.puts == [[(2, 6)]]
    assert "остатки на Авито" in capsys.readouterr().out
    fake.calls.clear()
    avito_stock.after_cycle(world.conn, box, world.root, unload({"101": 7, "102": 3}), [result(world, tripped=True)], http=fake, now=NOW)
    avito_stock.after_cycle(world.conn, box, world.root, unload({"101": 7, "102": 3}), [result(world, ok=False)], http=fake, now=NOW)
    assert fake.calls == []                                                    # a feed held by the guard or not published: nothing


def test_a_failure_after_the_cycle_is_a_warning_never_the_cycles(world, capsys):
    box, _ = _keys(world)
    feed(world, "101")
    ads(world, **{"101": 1})
    avito_stock.save_config(world.conn, world.account["id"], "all", "", "", NOW_ISO, "owner")

    def broken(method, url, headers, body):
        raise RuntimeError("boom")
    avito_stock.after_cycle(world.conn, box, world.root, unload({"101": 7}), [result(world)], http=broken, now=NOW, sleep=lambda s: None)
    assert "остатки на Авито не отправлены" in capsys.readouterr().err


def test_a_failure_after_the_cycle_never_takes_back_what_the_caller_wrote_before(world, capsys):
    """tails2 Ф8: the step rolls back only its own work; a write of the cycle left uncommitted before it stays."""
    box, _ = _keys(world)
    feed(world, "101")
    ads(world, **{"101": 1})
    avito_stock.save_config(world.conn, world.account["id"], "all", "", "", NOW_ISO, "owner")
    world.conn.execute("INSERT OR REPLACE INTO app_state VALUES ('the_cycles_own', 'kept')")       # not committed by the caller

    def broken(method, url, headers, body):
        raise RuntimeError("boom")
    avito_stock.after_cycle(world.conn, box, world.root, unload({"101": 7}), [result(world)], http=broken, now=NOW, sleep=lambda s: None)
    assert "остатки на Авито не отправлены" in capsys.readouterr().err
    assert store.get_state(world.conn, "the_cycles_own") == "kept"


# ---- the command ---------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def cmd(world, monkeypatch):
    from tires_sync import cycle
    _, key_file = _keys(world)
    feed(world, "100", "101")
    ads(world, **{"100": 1, "101": 2})
    cycle.save_check_inputs(world.conn, world.account["id"], world.root / world.account["feed_token"] / "avito_feed.xml", (), {})   # as a published cycle leaves it
    world.conn.commit()
    (world.tmp / "unload.xml").write_bytes(catalog_xml(commodity_xml(NNOMMODIF="100", NREST="9"), commodity_xml(NNOMMODIF="101", NREST="5")))
    fake = Avito(stock={1: 1, 2: 1})
    monkeypatch.setattr(cli, "_http", lambda: fake)
    base = ["stock-push", "--db", str(world.tmp / "t.db"), "--key-file", str(key_file), "--account", str(world.account["id"]),
            "--feed-root", str(world.root), "--unload", str(world.tmp / "unload.xml")]
    return SimpleNamespace(fake=fake, base=base, world=world)


def test_the_command_without_confirm_only_shows_even_for_an_account_switched_off(cmd, capsys):
    assert main(cmd.base) == 0
    out = capsys.readouterr().out
    assert "выключено" in out and "item_id 1" in out and "→ 9" in out and "ничего не отправлено" in out
    assert cmd.fake.writes() == []
    assert main(cmd.base + ["--dry-run", "--confirm"]) == 0
    assert cmd.fake.writes() == []


def test_the_command_with_confirm_sends_nothing_while_switched_off_and_sends_once_switched_on(cmd, capsys):
    assert main(cmd.base + ["--confirm"]) == 3
    assert cmd.fake.writes() == []
    conn = store.connect(cmd.world.tmp / "t.db")
    avito_stock.save_config(conn, cmd.world.account["id"], "list", "101", "", NOW_ISO, "owner")
    conn.commit()
    conn.close()
    assert main(cmd.base + ["--confirm"]) == 0
    assert cmd.fake.puts == [[(2, 5)]]


def test_the_command_info_only_reads(cmd, capsys):
    assert main(cmd.base + ["--info", "--limit", "1", "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "is_multiple" in out and "наш остаток 9" in out
    assert cmd.fake.writes() == [] and ("POST", "/stock-management/1/info") in cmd.fake.calls


# ---- the code behind an Id (a second account: own Ids `<code>_a2`, Ids taken over from the cabinet, the cycle's record) -----------------------

@pytest.fixture
def second(world):
    account_id = connections.create_account(world.conn, "Партнёр-2", NOW_ISO, "owner")
    connections.set_live(world.conn, account_id, True, NOW_ISO, "owner")
    account = connections.get_account(world.conn, account_id)
    (world.root / account["feed_token"]).mkdir(parents=True)
    return SimpleNamespace(conn=world.conn, account=account, root=world.root, tmp=world.tmp)


def test_a_second_account_reads_its_own_ids_and_its_bindings_and_never_guesses(second):
    from tires_sync import cycle
    store.set_state(second.conn, "adopted_%d" % second.account["id"], json.dumps({"ids": {"300": "555", "301": "k0alias00001"}, "images": {}, "shared": {}}))
    feed(second, "200_a2", "200-x4_a2", "555", "k0alias00001-x4", "777", "201", "k0inherit001")
    ads(second, **{"200_a2": 1, "200-x4_a2": 2, "555": 3, "k0alias00001-x4": 4, "777": 5, "201": 6, "k0inherit001": 7})
    stock = unload({"200": 9, "300": 6, "301": 8, "777": 40, "201": 40, "202": 12})
    p = lines_by_id(avito_stock.plan(second.conn, second.account, stock, second.root, cfg(), now=NOW))
    assert (p["200_a2"].code, p["200_a2"].quantity) == ("200", 9)
    assert (p["200-x4_a2"].code, p["200-x4_a2"].quantity) == ("200", 2)
    assert (p["555"].code, p["555"].quantity) == ("300", 6)                     # an Id of the cabinet: its bound position, not position 555
    assert (p["k0alias00001-x4"].code, p["k0alias00001-x4"].quantity) == ("301", 2)
    for ad_id in ("777", "201", "k0inherit001"):                                 # looks like a code, but not this account's form: no guess
        assert (p[ad_id].skip, p[ad_id].quantity) == (avito_stock.SKIP_NO_CODE, None)
    path = second.root / second.account["feed_token"] / "avito_feed.xml"
    cycle.save_check_inputs(second.conn, second.account["id"], path, (), {"k0inherit001": "202"})      # the cycle's record of this file
    p = lines_by_id(avito_stock.plan(second.conn, second.account, stock, second.root, cfg(), now=NOW))
    assert (p["k0inherit001"].code, p["k0inherit001"].quantity) == ("202", 12)
    feed(second, "200_a2", "k0inherit001")                                       # another file now: that record is not about it
    p = lines_by_id(avito_stock.plan(second.conn, second.account, stock, second.root, cfg(), now=NOW))
    assert p["k0inherit001"].skip == avito_stock.SKIP_NO_CODE


def test_an_ad_of_the_cabinet_not_seen_by_its_last_reading_is_not_bound(world):
    feed(world, "101")
    ads(world, **{"101": 1})
    world.conn.execute("INSERT INTO avito_ad (account_id, avito_id, ad_id, title, status, seen_at) VALUES (?, 2, '101', '', 'active', '2026-10-01T04:20:00')",
                       (world.account["id"],))                                   # closed since: its row still says active
    p = lines_by_id(avito_stock.plan(world.conn, world.account, unload({"101": 5}), world.root, cfg(), now=NOW))
    assert (p["101"].avito_id, p["101"].quantity) == (1, 5)


# ---- the command: a dormant account, what --restore cannot give back -------------------------------------------------------------------------

def test_the_command_sends_nothing_to_an_account_whose_publication_is_off(cmd, capsys):
    conn = store.connect(cmd.world.tmp / "t.db")
    avito_stock.save_config(conn, cmd.world.account["id"], "all", "", "", NOW_ISO, "owner")
    connections.set_live(conn, cmd.world.account["id"], False, NOW_ISO, "owner")
    conn.commit()
    conn.close()
    assert main(cmd.base + ["--confirm"]) == 3
    assert cmd.fake.calls == [] and "публикация аккаунта" in capsys.readouterr().out


def test_restore_says_how_many_it_cannot_give_back(cmd, capsys):
    conn = store.connect(cmd.world.tmp / "t.db")
    conn.execute("INSERT INTO avito_stock (account_id, avito_id, ad_id, code, quantity, status, at, before_quantity, before_at) "
                 "VALUES (?, 1, '100', '100', 9, 'sent', ?, NULL, ?)", (cmd.world.account["id"], NOW_ISO, NOW_ISO))
    conn.commit()
    conn.close()
    assert main(cmd.base + ["--restore"]) == 0
    out = capsys.readouterr().out
    assert "вернуть прежние числа: 0" in out and "без ограничения" in out and ": 1 объявлений" in out
    assert cmd.fake.writes() == []


# ---- the hook in run-cycle ------------------------------------------------------------------------------------------------------------------

REF = '<Shiny>\n<make name="Ikon Tyres">\n<model name="Test Model"/>\n</make>\n</Shiny>'


def _cabinet(w):
    connections.set_avito_identity(w.conn, w.account["id"], 80000001, "Основной", "79000000000")     # as «Проверить подключение» records it


def _published(w):
    return (w.root / w.account["feed_token"] / "avito_feed.xml").exists()


def _cycle_args(tmp, key_file, rest="10"):
    (tmp / "unload.xml").write_bytes(catalog_xml(commodity_xml(NREST=rest)))
    (tmp / "reference.txt").write_text(REF, encoding="utf-8")
    return ["--db", str(tmp / "t.db"), "--unload", str(tmp / "unload.xml"), "--reference", str(tmp / "reference.txt"),
            "--feed-root", str(tmp / "feed"), "--key-file", str(key_file)]


def test_run_cycle_sends_the_numbers_only_when_it_publishes(world, monkeypatch):
    _, key_file = _keys(world)
    _cabinet(world)
    avito_stock.save_config(world.conn, world.account["id"], "list", "100", "", NOW_ISO, "owner")
    ads(world, **{"100": 1})
    world.conn.commit()
    fake = Avito(stock={1: 1})
    monkeypatch.setattr(cli, "_http", lambda: fake)
    stock_calls = lambda: [c for c in fake.calls if c[1].startswith("/stock-management/")]
    main(["run-cycle"] + _cycle_args(world.tmp, key_file))
    assert stock_calls() == [] and not _published(world)                       # a draft only: nothing published, nothing sent
    main(["run-cycle", "--publish"] + _cycle_args(world.tmp, key_file))
    assert _published(world) and fake.puts == [[(1, 10)]]
    main(["run-cycle"] + _cycle_args(world.tmp, key_file, rest="7"))           # the stock fell, but this run publishes nothing
    assert fake.puts == [[(1, 10)]]
    main(["run-cycle", "--publish"] + _cycle_args(world.tmp, key_file, rest="7"))
    assert fake.puts == [[(1, 10)], [(1, 7)]]


def test_run_cycle_with_the_switch_off_asks_avito_nothing_about_stock(world, monkeypatch):
    _, key_file = _keys(world)
    _cabinet(world)
    ads(world, **{"100": 1})
    world.conn.commit()
    fake = Avito(stock={1: 1})
    monkeypatch.setattr(cli, "_http", lambda: fake)
    main(["run-cycle", "--publish"] + _cycle_args(world.tmp, key_file))
    assert _published(world)
    assert [c for c in fake.calls if c[1].startswith(("/stock-management/", "/autoload/v2/items/avito_ids"))] == []


def test_the_command_refuses_to_send_when_the_last_cycle_built_another_file(cmd, capsys):
    conn = store.connect(cmd.world.tmp / "t.db")
    avito_stock.save_config(conn, cmd.world.account["id"], "all", "", "", NOW_ISO, "owner")
    conn.commit()
    conn.close()
    feed(cmd.world, "100", "101", "102")                                       # published by hand after the cycle's record
    assert main(cmd.base) == 0 and "внимание" in capsys.readouterr().out
    assert main(cmd.base + ["--confirm"]) == 3
    assert cmd.fake.writes() == [] and "сначала цикл с публикацией" in capsys.readouterr().out


def test_restore_works_for_an_account_whose_publication_is_off(cmd, capsys):
    conn = store.connect(cmd.world.tmp / "t.db")
    conn.execute("INSERT INTO avito_stock (account_id, avito_id, ad_id, code, quantity, status, at, before_quantity, before_at) "
                 "VALUES (?, 1, '100', '100', 9, 'sent', ?, 1, ?)", (cmd.world.account["id"], NOW_ISO, NOW_ISO))
    connections.set_live(conn, cmd.world.account["id"], False, NOW_ISO, "owner")
    conn.commit()
    conn.close()
    assert main(cmd.base + ["--restore", "--confirm"]) == 0
    assert cmd.fake.puts == [[(1, 1)]]


# ---- a method Avito refuses as such, keys refused with another status, a deleted ad in --restore ------------------------------------------------

def test_a_run_of_refused_readings_stops_the_pass_instead_of_one_request_per_ad(world):
    ids = [str(1000 + i) for i in range(30)]
    feed(world, *ids)
    ads(world, **{code: int(code) for code in ids})
    fake = Avito(stock={})                                                     # every reading: 400
    p = avito_stock.plan(world.conn, world.account, unload({code: 5 for code in ids}), world.root, cfg(), now=NOW)
    assert avito_stock.push(world.conn, client(fake), world.account, p.lines, NOW)["not_sent"] == 30
    assert sum(1 for c in fake.calls if c[1] == "/stock-management/1/info") == 1 + avito_stock.REFUSAL_STOP
    assert fake.writes() == []


def test_a_run_of_refused_writes_stops_the_pass(world):
    ids = [str(1000 + i) for i in range(30)]
    feed(world, *ids)
    ads(world, **{code: int(code) for code in ids})
    for code in ids:
        world.conn.execute("INSERT INTO avito_stock (account_id, avito_id, ad_id, code, status, before_quantity, before_at) VALUES (?,?,?,?,?,?,?)",
                           (world.account["id"], int(code), code, code, "not_sent", 1, NOW_ISO))
    fake = Avito(stock={int(code): 1 for code in ids}, dead=[int(code) for code in ids])
    p = avito_stock.plan(world.conn, world.account, unload({code: 5 for code in ids}), world.root, cfg(), now=NOW)
    counts = avito_stock.push(world.conn, client(fake), world.account, p.lines, NOW)
    assert len(fake.writes()) == 1 + avito_stock.REFUSAL_STOP
    assert counts == {"sent": 0, "failed": avito_stock.REFUSAL_STOP, "unknown": 0, "not_sent": 30 - avito_stock.REFUSAL_STOP}


def test_keys_refused_with_any_status_are_not_taken_for_refused_ads(world):
    feed(world, "101", "102")
    ads(world, **{"101": 1, "102": 2})
    fake = Avito(stock={1: 1, 2: 1}, token=404)
    p = avito_stock.plan(world.conn, world.account, unload({"101": 5, "102": 6}), world.root, cfg(), now=NOW)
    assert avito_stock.push(world.conn, client(fake), world.account, p.lines, NOW) == {"sent": 0, "failed": 0, "unknown": 0, "not_sent": 2}
    assert fake.calls == [("POST", "/token/")]


def test_restore_gives_back_the_others_when_one_ad_was_deleted_meanwhile(world):
    for avito_id, before in [(1, 3), (2, 4), (3, 5)]:
        world.conn.execute("INSERT INTO avito_stock (account_id, avito_id, ad_id, code, quantity, status, at, before_quantity, before_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?)", (world.account["id"], avito_id, str(avito_id), str(avito_id), 9, "sent", NOW_ISO, before, NOW_ISO))
    fake = Avito(stock={1: 9, 2: 9, 3: 9}, dead=[2])
    assert avito_stock.restore(world.conn, client(fake), world.account, NOW) == {"restored": 2, "failed": 1, "unknown": 0, "not_sent": 0}
    assert fake.puts == [[(1, 3), (2, 4), (3, 5)], [(1, 3)], [(2, 4)], [(3, 5)]]
    assert world.conn.execute("SELECT avito_id, status FROM avito_stock ORDER BY avito_id").fetchall() == [(1, "restored"), (2, "failed"), (3, "restored")]


# ---- the light pass «stock only» (run-cycle --stock-only) --------------------------------------------------------------------------------------

def _rows(*items):
    return catalog_xml(*(commodity_xml(TERRITORY_RN="T1", **kw) for kw in items))


FIVE = _rows(*(dict(NNOMMODIF=str(n)) for n in range(101, 106)))


def _pass_world(world, monkeypatch, mode):
    _, key_file = _keys(world)
    _cabinet(world)
    if mode != "off":
        avito_stock.save_config(world.conn, world.account["id"], mode, "", "", NOW_ISO, "owner")
    ads(world, **{str(n): n for n in range(101, 106)})
    world.conn.commit()
    fake = Avito(stock={n: 1 for n in range(101, 106)})
    monkeypatch.setattr(cli, "_http", lambda: fake)
    (world.tmp / "reference.txt").write_text(REF, encoding="utf-8")

    def run(unload, *extra, publish=True):
        (world.tmp / "unload.xml").write_bytes(unload)
        return main(["run-cycle"] + (["--publish"] if publish else []) + list(extra) + ["--db", str(world.tmp / "t.db"), "--unload", str(world.tmp / "unload.xml"),
                    "--reference", str(world.tmp / "reference.txt"), "--feed-root", str(world.root), "--key-file", str(key_file)])
    return fake, run


def test_a_pass_that_published_sends_the_numbers(world, monkeypatch):
    fake, run = _pass_world(world, monkeypatch, "all")
    assert run(FIVE) == 0 and fake.puts == [[(n, 10) for n in range(101, 106)]]           # the hourly cycle
    assert run(_rows(dict(NNOMMODIF="101", NREST="2"), *(dict(NNOMMODIF=str(n), NREST="7" if n == 103 else "10") for n in range(102, 106))),
               "--stock-only") == 0
    assert not (world.root / world.account["feed_token"] / "avito_feed.xml").read_text(encoding="utf-8").count("<Id>101</Id>")   # 101 taken off
    assert sorted(fake.puts[-1]) == [(101, 0), (103, 7)]                                   # 101 left the feed: 0; 103 fell to 7


def test_a_pass_that_published_nothing_sends_nothing(world, monkeypatch):
    fake, run = _pass_world(world, monkeypatch, "all")
    run(FIVE)
    feed_now = (world.root / world.account["feed_token"] / "avito_feed.xml").read_bytes()
    calls = len(fake.calls)
    fallen = _rows(dict(NNOMMODIF="101", NREST="2"), *(dict(NNOMMODIF=str(n), NREST="7") for n in range(102, 106)))
    assert run(fallen, "--stock-only", publish=False) == 0                                # takes 101 off in its draft, publishes nothing
    assert run(_rows(*(dict(NNOMMODIF=str(n), NREST="7") for n in range(101, 106))), "--stock-only") == 0     # nothing to take off
    assert (world.root / world.account["feed_token"] / "avito_feed.xml").read_bytes() == feed_now
    assert [c for c in fake.calls[calls:] if c[1].startswith(("/stock-management/", "/autoload/v2/items/avito_ids"))] == []


def test_a_pass_that_published_with_the_switch_off_asks_avito_nothing_about_stock(world, monkeypatch):
    fake, run = _pass_world(world, monkeypatch, "off")
    run(FIVE)
    assert run(_rows(dict(NNOMMODIF="101", NREST="2"), *(dict(NNOMMODIF=str(n)) for n in range(102, 106))), "--stock-only") == 0
    assert "<Id>101</Id>" not in (world.root / world.account["feed_token"] / "avito_feed.xml").read_text(encoding="utf-8")
    assert [c for c in fake.calls if c[1].startswith(("/stock-management/", "/autoload/v2/items/avito_ids"))] == []
