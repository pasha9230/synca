"""4tochki catalog: the search and the cards in the shape of the supplier's schema (tests/fixtures/tochki/*.xsd, taken from its WSDL on
2026-10-21), the hourly load with its snapshot on disk, and the positions it gives."""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from tires_sync import connections, store, tochki_feed
from tires_sync.models import Season
from tires_sync.secretbox import SecretBox
from tires_sync.tochki_client import ARRAYS_NS, NAMESPACE, SEARCH_NS, TochkiClient, TochkiError, find_tyre_xml, goods_info_xml

# the supplier's schemas are third-party and stay out of the public copy (owner, 2026-10-25): here in tests/fixtures/tochki/, in
# private/tests/fixtures/tochki/ once the code is open; a copy without them skips the schema test, unless TIRES_REQUIRE_PRIVATE_DOCS=1
ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIRS = [ROOT / "tests" / "fixtures" / "tochki", ROOT / "private" / "tests" / "fixtures" / "tochki"]
REQUIRE_ENV = "TIRES_REQUIRE_PRIVATE_DOCS"
XS = "{http://www.w3.org/2001/XMLSchema}"
COMMON_NS = "http://schemas.datacontract.org/2004/07/TS3.Domain.Models.Client.ClientSoapService"
INFO_NS = "http://schemas.datacontract.org/2004/07/TS3.Domain.Models.Client.ClientSoapService.SearchProductsDetailedInfo"
WH_NS = "http://schemas.datacontract.org/2004/07/TS3.Domain.Models.Client.ClientSoapService.GetWarehouses"
NIL = 'xmlns:i="http://www.w3.org/2001/XMLSchema-instance"'
NOW = datetime(2026, 10, 21, 10, 0, 0)


def envelope(inner):
    return '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>%s</s:Body></s:Envelope>' % inner


def item(code, rest=12, price="4500.00", wrh=7, quality=0, season="w", thorn="false", name=None, marka="Ikon Tyres", model="Test Model",
         my="", pish="https://img.4tochki.test/p.jpg", other_wrh=None):
    stocks = [(wrh, rest, price)] + ([other_wrh] if other_wrh else [])
    whpr = "".join("<b:wh_price_rest><b:price>%s</b:price><b:price_rozn>9999.00</b:price_rozn><b:rest>%d</b:rest><b:wrh>%d</b:wrh>"
                   "</b:wh_price_rest>" % (p, r, w) for w, r, p in stocks)
    return {"code": code, "xml": (
        "<a:TyrePriceRest><a:code>%s</a:code><a:img_big_my>%s</a:img_big_my><a:img_big_pish>%s</a:img_big_pish><a:img_small i:nil=\"true\"/>"
        "<a:marka>%s</a:marka><a:model>%s</a:model><a:name>%s</a:name><a:quality>%d</a:quality><a:saleId i:nil=\"true\"/>"
        "<a:season>%s</a:season><a:thorn>%s</a:thorn><a:type>car</a:type><a:whpr xmlns:b=\"%s\">%s</a:whpr></a:TyrePriceRest>") % (
        code, my, pish, marka, model, name or "%s %s 205/55 R16 94T" % (marka, model), quality, season, thorn, COMMON_NS, whpr)}


def card(code, width="205.00", height="55.00", diameter="16.00", load="94", speed="T", puncture="", xl="false", omolog=""):
    return ("<a:TyreContainer><a:brand>Ikon Tyres</a:brand><a:code>%s</a:code><a:diameter>%s</a:diameter><a:height>%s</a:height>"
            "<a:load_index>%s</a:load_index><a:model>Test Model</a:model><a:name>card name</a:name><a:omolog>%s</a:omolog>"
            "<a:puncture>%s</a:puncture><a:speed_index>%s</a:speed_index><a:strengthening>%s</a:strengthening><a:thorn>false</a:thorn>"
            "<a:width>%s</a:width></a:TyreContainer>") % (code, diameter, height, load, omolog, puncture, speed, xl, width)


class FakeTochki:
    """The supplier as its schema describes it: answers by SOAPAction, pages the search, and checks the shape of every request."""

    def __init__(self, items=(), cards=None, warehouses=(("7", "Санкт-Петербург 2", "true"), ("8", "Санкт-Петербург 21", "true"))):
        self.items = list(items)
        self.cards = dict(cards if cards is not None else {i["code"]: card(i["code"]) for i in self.items})
        self.warehouses = warehouses
        self.calls = []
        self.fail = {}

    def __call__(self, method, url, headers, body):
        action = headers["SOAPAction"].strip('"').rsplit("/", 1)[-1]
        request = ET.fromstring(body)[0][0]
        assert request.tag == "{%s}%s" % (NAMESPACE, action)
        self.calls.append((action, request))
        if action in self.fail:
            return self.fail[action]
        if action == "GetWarehouses":
            rows = "".join("<a:WarehouseInfo><a:id>%s</a:id><a:name>%s</a:name><a:haveDelivery>%s</a:haveDelivery></a:WarehouseInfo>" % w
                           for w in self.warehouses)
            inner = ('<GetWarehousesResponse xmlns="%s"><GetWarehousesResult xmlns:a="%s" %s><a:error i:nil="true"/><a:success>true</a:success>'
                     '<a:warehouses>%s</a:warehouses></GetWarehousesResult></GetWarehousesResponse>') % (NAMESPACE, WH_NS, NIL, rows)
        elif action == "GetFindTyre":
            page = int(request.find("{%s}page" % NAMESPACE).text)
            size = int(request.find("{%s}pageSize" % NAMESPACE).text)
            chunk = self.items[page * size:(page + 1) * size]
            pages = max(1, -(-len(self.items) // size))
            inner = ('<GetFindTyreResponse xmlns="%s"><GetFindTyreResult xmlns:a="%s" %s><a:currencyRate i:nil="true"/><a:error i:nil="true"/>'
                     '<a:price_rest_list>%s</a:price_rest_list><a:totalPages>%d</a:totalPages><a:warehouseLogistics i:nil="true"/>'
                     '</GetFindTyreResult></GetFindTyreResponse>') % (NAMESPACE, SEARCH_NS, NIL, "".join(i["xml"] for i in chunk), pages)
        elif action == "GetGoodsInfo":
            codes = [n.text for n in request.iter("{%s}string" % ARRAYS_NS)]
            assert 0 < len(codes) <= 200
            inner = ('<GetGoodsInfoResponse xmlns="%s"><GetGoodsInfoResult xmlns:a="%s" %s><a:error i:nil="true"/><a:tyreList>%s</a:tyreList>'
                     '</GetGoodsInfoResult></GetGoodsInfoResponse>') % (NAMESPACE, INFO_NS, NIL, "".join(self.cards[c] for c in codes if c in self.cards))
        else:
            raise AssertionError(action)
        return 200, {}, envelope(inner).encode("utf-8")

    def count(self, action):
        return sum(1 for a, _ in self.calls if a == action)


def schema_path(fixture, dirs=None):
    found = [d / fixture for d in (SCHEMA_DIRS if dirs is None else dirs) if (d / fixture).exists()]
    if not found:
        why = "the supplier's schema %s is private (tests/fixtures/tochki/ or private/…), not in this copy" % fixture
        if os.environ.get(REQUIRE_ENV) == "1":
            pytest.fail(f"{why}, and {REQUIRE_ENV}=1 says it must be here")
        pytest.skip(why)
    return found[0]


def test_a_missing_schema_skips_in_a_public_copy_and_fails_where_it_is_required(tmp_path, monkeypatch):
    nowhere = [tmp_path / "a", tmp_path / "private" / "a"]
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        schema_path("methods.xsd", nowhere)
    monkeypatch.setenv(REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match=REQUIRE_ENV):
        schema_path("methods.xsd", nowhere)
    nowhere[1].mkdir(parents=True)
    (nowhere[1] / "methods.xsd").write_text("<x/>", encoding="utf-8")       # private/… is enough once the code is open
    assert schema_path("methods.xsd", nowhere) == nowhere[1] / "methods.xsd"


def schema_sequence(fixture, name):
    root = ET.parse(schema_path(fixture)).getroot()
    for node in root:
        if node.get("name") == name:
            return [e.get("name") for e in node.iter(XS + "element")][1 if node.tag == XS + "element" else 0:]
    raise AssertionError(name)


def wrap(method, extra):
    return ET.fromstring('<%s xmlns="%s"><login>u</login><password>p</password>%s</%s>' % (method, NAMESPACE, extra, method))


def test_requests_follow_the_suppliers_schema():
    find = wrap("GetFindTyre", find_tyre_xml(7, 3, 2000))
    assert [c.tag.split("}")[1] for c in find] == schema_sequence("methods.xsd", "GetFindTyre")
    filter_ = find.find("{%s}filter" % NAMESPACE)
    members = [c.tag for c in filter_]
    assert all(t.startswith("{%s}" % SEARCH_NS) for t in members)
    order = schema_sequence("search_tires.xsd", "FindTyreFilter")
    names = [t.split("}")[1] for t in members]
    assert names == ["type_list", "wrh_list"] and names == sorted(names, key=order.index)       # the order matters to the service
    assert [n.text for n in filter_.iter("{%s}string" % ARRAYS_NS)] == ["car"] and [n.text for n in filter_.iter("{%s}int" % ARRAYS_NS)] == ["7"]
    info = wrap("GetGoodsInfo", goods_info_xml(["A&1", "B"]))
    assert [c.tag.split("}")[1] for c in info] == schema_sequence("methods.xsd", "GetGoodsInfo")
    assert [n.text for n in info.iter("{%s}string" % ARRAYS_NS)] == ["A&1", "B"]
    assert {"code", "whpr", "quality", "img_big_my", "img_big_pish", "marka", "model", "season", "thorn"} <= set(schema_sequence("search_tires.xsd", "TyrePriceRest"))
    assert {"price", "rest", "wrh"} <= set(schema_sequence("common.xsd", "wh_price_rest"))
    assert {"id", "name", "shortName", "haveDelivery"} <= set(schema_sequence("warehouses.xsd", "WarehouseInfo"))
    assert {"width", "height", "diameter", "load_index", "speed_index", "puncture", "strengthening", "omolog"} <= set(
        schema_sequence("goods_info.xsd", "TyreContainer"))


def test_the_warehouse_is_found_by_its_exact_name():
    http = FakeTochki()
    assert TochkiClient("u", "p", http=http).warehouse_id() == 7
    http = FakeTochki(warehouses=(("8", "Санкт-Петербург 21", "true"),))
    with pytest.raises(TochkiError, match="не найден"):
        TochkiClient("u", "p", http=http).warehouse_id()


def test_a_supplier_error_is_reported_without_the_credentials():
    http = FakeTochki()
    bad = ('<GetFindTyreResponse xmlns="%s"><GetFindTyreResult xmlns:a="%s" %s><a:error xmlns:b="%s"><b:code>30</b:code>'
           '<b:comment>login secret-login denied</b:comment></a:error><a:totalPages>0</a:totalPages></GetFindTyreResult></GetFindTyreResponse>') % (
        NAMESPACE, SEARCH_NS, NIL, COMMON_NS)
    http.fail["GetFindTyre"] = (200, {}, envelope(bad).encode())
    with pytest.raises(TochkiError) as err:
        TochkiClient("secret-login", "p", http=http).find_tyre_page(7, 0)
    assert "30" in str(err.value) and "secret-login" not in str(err.value)


def setup_db(tmp_path, supplier=True, password="pw-SECRET"):
    conn = store.connect(tmp_path / "t.db")
    box = SecretBox(Fernet.generate_key())
    sid = connections.create_supplier(conn, "4Точки", "2026-10-21T00:00:00", "o") if supplier else None
    connections.set_config(conn, "tochki", 0, {"login": "api-user", "supplier_id": sid, "feed": False}, "2026-10-21T00:00:00", "o")
    if password:
        connections.set_secret(conn, box, "tochki", 0, "password", password, "2026-10-21T00:00:00", "o")
    conn.commit()
    return conn, box, tmp_path / "unload.xml"


class Clock:
    def __init__(self, step=0.0):
        self.now, self.step, self.sleeps = 0.0, step, []

    def __call__(self):
        self.now += self.step
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def refresh(conn, box, base, http, now=NOW, clock=None):
    clock = clock or Clock()
    return tochki_feed.refresh(conn, box, base, now, http=http, sleep=clock.sleep, clock=clock)


def test_the_load_walks_every_page_asks_cards_once_and_keeps_a_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr("tires_sync.tochki_client.PAGE_SIZE", 2)
    items = [item("C%d" % n, rest=41 if n == 0 else 5 + n) for n in range(5)]
    items.append(item("BAD", quality=2))                                                    # broken goods are never offered
    items.append(item("ELSE", wrh=9))                                                       # not on our warehouse
    items.append(item("C1", rest=99))                                                       # repeated on a later page: the first one counts
    http = FakeTochki(items)
    conn, box, base = setup_db(tmp_path)
    clock = Clock()
    result = refresh(conn, box, base, http, clock=clock)
    assert result["status"] == "ok" and "5" in result["message"]
    assert http.count("GetFindTyre") == 4 and http.count("GetGoodsInfo") == 1 and http.count("GetWarehouses") == 1
    assert all(s == tochki_feed.PAUSE_SECONDS for s in clock.sleeps) and len(clock.sleeps) == 4      # a pause between every two calls
    for _, request in http.calls:
        assert b"pw-SECRET" not in ET.tostring(request) or request.find("{%s}password" % NAMESPACE).text == "pw-SECRET"
    snapshot = json.loads((tmp_path / "tochki-snapshot.json").read_text(encoding="utf-8"))
    assert [r["code"] for r in snapshot["items"]] == ["C0", "C1", "C2", "C3", "C4"] and snapshot["warehouse_id"] == 7
    state = tochki_feed.read_state(base)
    assert state["items"] == 5 and state["skipped_quality"] == 1 and state["error"] == "" and state["ok_at"] == NOW.isoformat()
    for path in tmp_path.iterdir():
        assert "pw-SECRET" not in path.read_text(encoding="utf-8", errors="replace") if path.suffix == ".json" else True

    again = FakeTochki(items)
    assert refresh(conn, box, base, again, now=NOW + timedelta(minutes=30))["status"] == "wait" and again.calls == []      # once an hour
    assert refresh(conn, box, base, again, now=NOW + timedelta(minutes=56))["status"] == "ok"
    assert again.count("GetGoodsInfo") == 0                                                  # the cards are known


def test_positions_are_offers_of_the_chosen_supplier(tmp_path):
    items = [item("C1", rest=41, price="4500.50", name="Ikon Tyres Test Model 205/55 R16 94T XL"), item("C2", season="s", thorn="false"),
             item("C3", thorn="true", my="https://img.4tochki.test/my.jpg"), item("C4"), item("C5"), item("C6")]
    cards = {"C1": card("C1", xl="true", omolog="MO"), "C2": card("C2", puncture="RunFlat"), "C3": card("C3"),
             "C4": card("C4", width="10.50"), "C6": card("C6", load="", speed="")}
    conn, box, base = setup_db(tmp_path)
    refresh(conn, box, base, FakeTochki(items, cards))
    sid = tochki_feed.config(conn)["supplier_id"]
    parsed, info = tochki_feed.load_snapshot(base, sid)
    one = parsed.offers["s%d-C1" % sid]
    assert (one.brand_key, one.model_raw, one.season, one.width, one.height, one.diameter, one.load, one.speed) == (
        "S%d:IKON TYRES" % sid, "Test Model", Season.WINTER, 205, 55, 16, "94", "T")
    assert one.stock == 41 and one.base_price == 4500.5 and one.code == "" and one.image == "https://img.4tochki.test/p.jpg"
    assert one.homologation == "MO" and "XL" in one.name and not one.runflat and not one.studded and one.territory_rn == "7"
    assert parsed.offers["s%d-C2" % sid].runflat and parsed.offers["s%d-C2" % sid].season == Season.SUMMER
    assert parsed.offers["s%d-C3" % sid].studded and parsed.offers["s%d-C3" % sid].image == "https://img.4tochki.test/my.jpg"
    assert parsed.issues["s%d-C4" % sid] == ["bad_width:10.50"] and parsed.issues["s%d-C5" % sid][0] == "no_card"
    assert parsed.offers["s%d-C6" % sid].load == ""                       # no indices: the position waits for its pair like any other
    assert info["warehouse_id"] == 7


@pytest.mark.parametrize("trouble", ["down", "fault", "empty", "shrunk", "cut"])
def test_a_failed_load_keeps_the_previous_positions(tmp_path, trouble):
    conn, box, base = setup_db(tmp_path)
    assert refresh(conn, box, base, FakeTochki([item("C%d" % n) for n in range(10)]))["status"] == "ok"
    before = (tmp_path / "tochki-snapshot.json").read_bytes()
    http = FakeTochki([item("C%d" % n) for n in range(10)])
    if trouble == "down":
        def http(*_):
            raise OSError("timed out")
    elif trouble == "fault":
        http.fail["GetFindTyre"] = (500, {}, envelope('<s:Fault><faultstring>pw-SECRET</faultstring></s:Fault>').encode())
    elif trouble == "empty":
        http.items = []
    elif trouble == "shrunk":
        http.items = http.items[:4]
    elif trouble == "cut":
        http.fail["GetFindTyre"] = (200, {}, b"<s:Envelope")
    result = refresh(conn, box, base, http, now=NOW + timedelta(hours=1))
    assert result["status"] == "error" and "прежние" in result["message"] and "pw-SECRET" not in result["message"]
    assert (tmp_path / "tochki-snapshot.json").read_bytes() == before
    state = tochki_feed.read_state(base)
    assert state["error"] and state["ok_at"] == NOW.isoformat() and state["items"] == 10


def test_a_long_walk_stops_and_cards_wait_for_the_next_hour(tmp_path, monkeypatch):
    monkeypatch.setattr("tires_sync.tochki_client.CARDS_PER_CALL", 2)
    monkeypatch.setattr("tires_sync.tochki_feed.CARDS_PER_CALL", 2)
    conn, box, base = setup_db(tmp_path)
    http = FakeTochki([item("C%d" % n) for n in range(6)])
    clock = Clock()
    real = http.__call__

    def slow(*args):
        clock.now += 50                                    # every call takes 50 seconds: the budget of 3 minutes ends among the cards
        return real(*args)
    assert refresh(conn, box, base, slow, clock=clock)["status"] == "ok"
    state = tochki_feed.read_state(base)
    assert 0 < state["without_card"] < 6 and http.count("GetGoodsInfo") < 3
    assert refresh(conn, box, base, http, now=NOW + timedelta(hours=1))["status"] == "ok"
    assert tochki_feed.read_state(base)["without_card"] == 0

    many = FakeTochki([item("C%d" % n) for n in range(6)])
    monkeypatch.setattr("tires_sync.tochki_client.PAGE_SIZE", 1)
    clock = Clock(step=400)
    result = refresh(conn, box, base, many, now=NOW + timedelta(hours=2), clock=clock)
    assert result["status"] == "error" and "не уложился" in result["message"]


def test_nothing_is_asked_until_the_supplier_and_the_password_are_set(tmp_path):
    for supplier, password in ((False, "pw"), (True, "")):
        folder = tmp_path / ("%s%s" % (supplier, bool(password)))
        folder.mkdir()
        conn, box, base = setup_db(folder, supplier=supplier, password=password)
        http = FakeTochki([item("C1")])
        assert refresh(conn, box, base, http)["status"] == "off" and http.calls == []
        assert not (folder / "tochki-snapshot.json").exists()


def test_the_supplier_number_is_read_strictly(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    for raw, expected in (({"supplier_id": 2}, 2), ({"supplier_id": 1}, None), ({"supplier_id": "2"}, None), ({"supplier_id": True}, None),
                          ({}, None)):
        connections.set_config(conn, "tochki", 0, dict(raw, feed="yes"), "2026-10-21T00:00:00", "o")
        cfg = tochki_feed.config(conn)
        assert cfg["supplier_id"] == expected and cfg["feed"] is False


def test_an_unexpected_failure_is_kept_in_the_state_by_its_kind_only(tmp_path, monkeypatch):
    conn, box, base = setup_db(tmp_path)

    def broken(self, *a, **k):
        raise KeyError("pw-SECRET")
    monkeypatch.setattr(TochkiClient, "find_tyre_page", broken)
    result = refresh(conn, box, base, FakeTochki([item("C1")]))
    state = tochki_feed.read_state(base)
    assert result["status"] == "error" and state["error"] == "KeyError" and "pw-SECRET" not in json.dumps(state)


def test_a_password_repeated_at_the_end_of_a_long_error_text_is_hidden_before_the_text_is_cut():
    http = FakeTochki()
    comment = "x" * 195 + " pw-SECRET-LONG"
    bad = ('<GetFindTyreResponse xmlns="%s"><GetFindTyreResult xmlns:a="%s" %s><a:error xmlns:b="%s"><b:code>50</b:code>'
           '<b:comment>%s</b:comment></a:error><a:totalPages>0</a:totalPages></GetFindTyreResult></GetFindTyreResponse>') % (
        NAMESPACE, SEARCH_NS, NIL, COMMON_NS, comment)
    http.fail["GetFindTyre"] = (200, {}, envelope(bad).encode())
    with pytest.raises(TochkiError) as err:
        TochkiClient("u", "pw-SECRET-LONG", http=http).find_tyre_page(7, 0)
    assert "pw-SEC" not in str(err.value)


def test_the_access_check_uses_the_exact_warehouse_name():
    from test_tochki import PING_TRUE
    http = FakeTochki(warehouses=(("8", "Санкт-Петербург 21", "true"),))
    calls = [PING_TRUE]

    def transport(method, url, headers, body):
        if headers["SOAPAction"].endswith('Ping"'):
            status, payload = calls[0]
            return status, {}, payload.encode()
        return http(method, url, headers, body)
    assert "не найден" in TochkiClient("u", "p", http=transport).check()
    http.warehouses = (("7", "Санкт-Петербург 2", "true"),)
    assert "виден (№7)" in TochkiClient("u", "p", http=transport).check()
