"""«№ Счета» of MIM's cabinet = «Номер перемещения» (webmim_cabinet, the owner 2026-11-09): read with the v3 session of the API user right
after the press, by the window «Подтверждение» and hourly; written once; the messages «заказ оформлен» that waited for it go by themselves,
once. Fakes only: no request leaves the tests."""
from __future__ import annotations

import json
import re
import urllib.parse

import pytest
from cryptography.fernet import Fernet

from panel_helpers import make_app, post, shared_mark_of
from test_order_messages import FakeVk, with_vk
from test_order_placed import Deliver, ready
from test_webmim_create import NOW, FakeWebmim, card, client_of, db, place, point
from tires_sync import (connections, order_engine as engine, order_placed, order_templates as templates, orders, store, users,
                        webmim_cabinet, webmim_create, webmim_orders)
from tires_sync.secretbox import SecretBox

INVOICE = "26R-1564565"
LATER = "2026-10-23T12:05:00"


def row(number, invoice=INVOICE, total=24000):
    return {"orderNumber": number, "paymentAccount": {"number": invoice} if invoice is not None else None, "invoiceDate": "2026-10-23T12:00:00",
            "status": "создан", "sum": total, "pack": "", "createdDate": "2026-10-23T12:00:00"}


def waits_for_the_number(conn):
    for name, audience in ((order_placed.CLIENT_TEMPLATE, "client"), (order_placed.POINT_TEMPLATE, "manager")):
        template = next(t for t in templates.list_templates(conn) if t["name"] == name)
        templates.update_template(conn, template["id"], name, audience, "Заказ {webmim_number}, перемещение {transfer_number}")


def placed_order(tmp_path, cabinet=None, server="prod", login="api-bot"):
    """A placed order on the live server whose messages to the client (Avito) and the point (VK) need the transfer number; on_placed done."""
    conn = db(tmp_path, server=server)
    if login != "api-bot":
        connections.set_config(conn, "webmim_orders", 0, {"server": server, "login": login}, NOW, "owner")
    mine = with_vk(conn, ready(conn))
    waits_for_the_number(conn)
    fake = FakeWebmim()
    fake.cabinet = cabinet
    client = client_of(fake)
    result = webmim_create.place(conn, mine, client, server, NOW, __import__("datetime").date(2026, 10, 23), "owner", sleep=lambda _s: None)
    client.close()                                                       # the route's finally: the press's logout
    assert result["state"] == "done"
    deliver, vk = Deliver(), FakeVk()
    lines = order_placed.on_placed(conn, mine, webmim_create.get_state(conn, mine)["webmim_id"], NOW, "owner", deliver, vk)
    return conn, mine, fake, deliver, vk, lines


def message_tasks(conn, mine):
    return [t for t in engine.list_tasks(conn, mine) if t["kind"] == "message"]


def events(conn, mine):
    return [(e["text"], e["level"]) for e in orders.list_events(conn, mine)]


class Cabinet:
    """The cabinet only (/api/v3): `login` the answer of the login, `listing` of the list ("net": the network fails), the logouts counted."""

    def __init__(self, rows=(), login=None, listing=200):
        self.rows, self.login, self.listing, self.calls, self.logouts = list(rows), login, listing, [], 0

    def __call__(self, method, url, headers, body, timeout=None):
        path = urllib.parse.urlsplit(url).path
        self.calls.append((method, path))
        if path == "/api/v3/auth/login":
            return self.login or (200, {}, json.dumps({"result": True, "authKey": "AUTH-SECRET"}).encode())
        if path == "/api/v3/auth/logout":
            self.logouts += 1
            return 200, {}, b"{}"
        assert headers["Authorization"] == "Bearer AUTH-SECRET" and method == "GET"
        if self.listing == "net":
            raise OSError("reset")
        if self.listing != 200:
            return self.listing, {}, b"{}"
        return 200, {}, json.dumps({"data": self.rows}).encode()


def cabinet_of(fake):
    return lambda: webmim_orders.OrdersClient("https://webmim.example", "api-bot", "pw-SECRET-1", http=fake)


def poll(conn, mine, fake, now=LATER, deliver=None, vk=None):
    deliver, vk = deliver or Deliver(), vk or FakeVk()
    return webmim_cabinet.poll(conn, mine, cabinet_of(fake), now,
                               lambda card_id: order_placed.send_waiting(conn, card_id, now, "owner", deliver, vk)), deliver, vk


# ---- right after the press -------------------------------------------------------------------------------------------------------------

def test_the_number_of_the_press_session_is_in_the_card_before_the_messages_and_both_go_once(tmp_path):
    conn, mine, fake, deliver, vk, lines = placed_order(tmp_path, cabinet=[row(5002), row(5001)])
    assert orders.get_card(conn, mine)["transfer_number"] == INVOICE
    assert ("Номер перемещения из кабинета MIM: %s (счёт от 23.10.2026)." % INVOICE, "grey") in events(conn, mine)
    assert len(deliver.sent) == 1 and len(vk.sent) == 1 and "перемещение " + INVOICE in deliver.sent[0][1]
    assert [c[:2] for c in fake.calls].count(("GET", "/api/v3/orders")) == 1 and fake.sessions == 0       # one read, logged out
    assert [c[:2] for c in fake.calls].count(("POST", "/api/v3/auth/login")) == 1
    again, _, _ = poll(conn, mine, Cabinet([row(5001)]), deliver=deliver, vk=vk)                           # the window, then a second tab
    assert again["state"] == "found" and again["result"] == "same" and len(deliver.sent) == 1 and len(vk.sent) == 1
    assert {t["status"] for t in message_tasks(conn, mine)} == {"done"}


def test_no_number_at_the_press_the_messages_wait_and_say_it_comes_from_the_cabinet(tmp_path):
    conn, mine, fake, deliver, vk, lines = placed_order(tmp_path, cabinet=[row(5001, invoice=None)])
    assert not deliver.sent and not vk.sent and orders.get_card(conn, mine)["transfer_number"] == ""
    assert all("уйдёт само, если номер перемещения придёт из кабинета MIM" in line for line in lines)
    assert {t["status"] for t in message_tasks(conn, mine)} == {"error"} and fake.sessions == 0


# ---- the window --------------------------------------------------------------------------------------------------------------------------

def test_the_window_finds_the_number_writes_it_and_both_messages_go_exactly_once(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    deliver, vk = Deliver(), FakeVk()
    cabinet = Cabinet([row(5001, invoice=None)])
    first, _, _ = poll(conn, mine, cabinet, "2026-10-23T12:01:00", deliver, vk)
    assert first["state"] == "wait" and not first["final"] and first["text"] == "№ счёта ещё не выдан" and not deliver.sent
    cabinet.rows = [row(50011), row(5001)]                               # 50011 is not 5001: the number is exact
    second, _, _ = poll(conn, mine, cabinet, "2026-10-23T12:01:10", deliver, vk)
    assert second["state"] == "found" and second["result"] == "written" and second["final"] and second["invoice"] == INVOICE
    assert second["sum"] == 24000 and second["purchase"] == 24000 and not second["sum_differs"]
    assert len(deliver.sent) == 1 and len(vk.sent) == 1 and len(second["lines"]) == 2
    # a second tab within the gap gets the last answer without a login; a later poll and the hourly reading write nothing, send nothing
    calls = len(cabinet.calls)
    tab, _, _ = poll(conn, mine, cabinet, "2026-10-23T12:01:12", deliver, vk)
    assert tab["state"] == "found" and tab["lines"] == [] and len(cabinet.calls) == calls
    later, _, _ = poll(conn, mine, cabinet, "2026-10-23T12:02:00", deliver, vk)     # the number is in: the answer found, no login
    assert later["state"] == "found" and later["lines"] == [] and len(cabinet.calls) == calls
    again = webmim_cabinet.take(conn, mine, webmim_cabinet.info(row(5001)), "2026-10-23T12:02:30",            # the hourly reading
                                lambda card_id: order_placed.send_waiting(conn, card_id, "2026-10-23T12:02:30", "owner", deliver, vk))
    assert again == ("same", []) and len(deliver.sent) == 1 and len(vk.sent) == 1
    assert order_placed.send_waiting(conn, mine, "2026-10-23T12:03:00", "owner", deliver, vk) == []     # «Досоздать», a second worker
    assert len(deliver.sent) == 1 and len(vk.sent) == 1
    assert [t for t, _ in events(conn, mine)].count("Номер перемещения из кабинета MIM: %s (счёт от 23.10.2026)." % INVOICE) == 1
    assert cabinet.logouts == 2 == cabinet.calls.count(("POST", "/api/v3/auth/login"))       # every login logged out


def test_the_sums_differ_are_shown_as_such(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    result, _, _ = poll(conn, mine, Cabinet([row(5001, total=25000)]))
    assert result["sum_differs"] and result["sum"] == 25000 and result["purchase"] == 24000


def test_a_number_typed_by_a_person_is_kept_and_nothing_is_sent(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    orders.update_card(conn, mine, {"transfer_number": "26R-1000001"}, NOW, "owner")
    result, deliver, vk = poll(conn, mine, Cabinet([row(5001)]))
    assert result["result"] == "differs" and result["transfer"] == "26R-1000001" and not deliver.sent and not vk.sent
    assert orders.get_card(conn, mine)["transfer_number"] == "26R-1000001"
    assert any(level == "yellow" and INVOICE in text for text, level in events(conn, mine))


def test_no_number_in_the_cabinet_leaves_everything_as_now(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    result, deliver, vk = poll(conn, mine, Cabinet([]))
    assert result["state"] == "wait" and not result["final"] and "ещё нет в кабинете MIM" in result["text"]
    assert not deliver.sent and {t["status"] for t in message_tasks(conn, mine)} == {"error"}


@pytest.mark.parametrize("login, listing, final, words, logouts", [
    ((200, {}, json.dumps({"result": False, "message": "Превышено количество подключений"}).encode()), 200, False, "лимит", 0),
    ((200, {}, json.dumps({"result": False, "message": "bad"}).encode()), 200, True, "неверный логин или пароль", 0),
    ((401, {}, b"{}"), 200, False, "код 401", 0),
    (None, 401, True, "не видит заказов кабинета", 1),
    (None, 403, True, "не видит заказов кабинета", 1),
    (None, 500, False, "код 500", 1),
    (None, "net", False, "кабинет недоступен (OSError)", 1),
])
def test_a_failure_is_a_grey_record_once_the_window_says_why_and_the_logout_always_goes(tmp_path, monkeypatch, login, listing, final, words, logouts):
    monkeypatch.setattr("tires_sync.reliability.default_sleep", lambda _s: None)
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    cabinet = Cabinet([row(5001)], login=login, listing=listing)
    result, deliver, _ = poll(conn, mine, cabinet)
    assert result["state"] == "error" and result["final"] is final and words in result["text"] and not deliver.sent
    assert cabinet.logouts == logouts
    poll(conn, mine, cabinet, "2026-10-23T12:06:00")
    grey = [t for t, level in events(conn, mine) if t.startswith("Номер перемещения из кабинета MIM не прочитан") and level == "grey"]
    assert len(grey) == 1 and words in grey[0]
    assert "pw-SECRET-1" not in json.dumps(result) and "AUTH-SECRET" not in json.dumps(result) and "AUTH-SECRET" not in grey[0]
    assert orders.get_card(conn, mine)["transfer_number"] == "" and {t["status"] for t in message_tasks(conn, mine)} == {"error"}


def test_the_test_server_never_takes_the_number(tmp_path):
    conn, mine, fake, deliver, vk, lines = placed_order(tmp_path, cabinet=[row(5001)], server="test")
    assert orders.get_card(conn, mine)["transfer_number"] == "" and ("GET", "/api/v3/orders") not in [c[:2] for c in fake.calls]
    cabinet = Cabinet([row(5001)])
    result, _, _ = poll(conn, mine, cabinet)
    assert result["state"] == "off" and cabinet.calls == [] and not deliver.sent
    assert "подготовится, когда впишете номер перемещения" in lines[0]


def test_the_live_server_does_not_take_it_for_an_order_made_on_the_test_one(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[], server="test")
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod", "login": "api-bot"}, NOW, "owner")
    cabinet = Cabinet([row(5001)])
    assert poll(conn, mine, cabinet)[0]["state"] == "off" and cabinet.calls == []


def test_an_empty_login_changes_nothing(tmp_path):
    conn, mine, fake, deliver, vk, lines = placed_order(tmp_path, cabinet=[row(5001)], login="")
    assert not webmim_cabinet.enabled(conn) and orders.get_card(conn, mine)["transfer_number"] == ""
    assert ("GET", "/api/v3/orders") not in [c[:2] for c in fake.calls] and not deliver.sent
    assert "подготовится, когда впишете номер перемещения" in lines[0]
    cabinet = Cabinet([row(5001)])
    assert poll(conn, mine, cabinet)[0]["state"] == "off" and cabinet.calls == []
    assert webmim_cabinet.safe_hourly(conn, cabinet_of(cabinet)(), "prod", LATER, None) == "" and cabinet.calls == []


def test_a_number_of_two_orders_is_never_written_by_the_window(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    other = card(conn, point_id=point(conn, name="Парнас", webmim_address_id="78"))
    orders.update_card(conn, other, {"webmim_number": orders.get_card(conn, mine)["webmim_number"]}, NOW, "owner")
    cabinet = Cabinet([row(5001)])
    result, deliver, _ = poll(conn, mine, cabinet)
    assert result["state"] == "off" and cabinet.calls == [] and orders.get_card(conn, mine)["transfer_number"] == "" and not deliver.sent


def test_an_order_not_placed_by_the_button_has_no_window_reading(tmp_path):
    conn = db(tmp_path, server="prod")
    mine = card(conn)
    orders.update_card(conn, mine, {"webmim_number": "5001"}, NOW, "owner")
    cabinet = Cabinet([row(5001)])
    assert poll(conn, mine, cabinet)[0]["state"] == "off" and cabinet.calls == [] and orders.get_card(conn, mine)["transfer_number"] == ""


def test_a_number_in_already_costs_no_login_and_an_old_answer_is_not_shown(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    cabinet = Cabinet([row(5001)])
    assert poll(conn, mine, cabinet, "2026-10-23T12:01:00")[0]["result"] == "written"
    calls = len(cabinet.calls)
    again, _, _ = poll(conn, mine, cabinet, "2026-10-23T12:30:00")             # a reload of the window: the found answer, no login
    assert again["state"] == "found" and again["invoice"] == INVOICE and len(cabinet.calls) == calls
    (tmp_path / "b").mkdir()
    conn2, other, *_ = placed_order(tmp_path / "b", cabinet=[])
    stale = Cabinet(listing=403)
    assert poll(conn2, other, stale, "2026-10-23T12:01:00")[0]["final"]
    conn2.execute("UPDATE app_state SET value = ? WHERE key = ?", (json.dumps({"at": "2026-10-23T12:20:00"}), webmim_cabinet.LAST))
    conn2.commit()
    fresh = poll(conn2, other, Cabinet([row(5001)]), "2026-10-23T12:20:02")[0]   # within the gap, but the stored answer is old
    assert fresh["state"] == "wait" and not fresh["final"]


# ---- hourly -----------------------------------------------------------------------------------------------------------------------------

def test_hourly_one_login_for_every_open_order_writes_and_sends_once(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    other = card(conn, point_id=point(conn, name="Парнас", webmim_address_id="78"))
    orders.update_card(conn, other, {"webmim_number": "7007"}, NOW, "owner")         # typed by hand, not placed by the button: no send
    deliver, vk = Deliver(), FakeVk()
    cabinet = Cabinet([row(7007, invoice="26R-7000007"), row(5001)])
    client = cabinet_of(cabinet)()
    with client:
        tail = webmim_cabinet.safe_hourly(conn, client, "prod", LATER,
                                          lambda card_id: order_placed.send_waiting(conn, card_id, LATER, "почасовое чтение WebMIM", deliver, vk))
    assert tail == "; кабинет MIM: вписано 2, сообщений отправлено 2"
    assert orders.get_card(conn, mine)["transfer_number"] == INVOICE and orders.get_card(conn, other)["transfer_number"] == "26R-7000007"
    assert len(deliver.sent) == 1 and len(vk.sent) == 1
    assert cabinet.calls.count(("POST", "/api/v3/auth/login")) == 1 and cabinet.logouts == 1
    client = cabinet_of(cabinet)()
    with client:                                                            # nothing waits: no login at all
        assert webmim_cabinet.safe_hourly(conn, client, "prod", LATER, None) == ""
    assert cabinet.calls.count(("POST", "/api/v3/auth/login")) == 1


def test_hourly_a_failed_send_is_not_counted_as_sent(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    client = cabinet_of(Cabinet([row(5001)]))()
    with client:
        tail = webmim_cabinet.safe_hourly(conn, client, "prod", LATER, lambda card_id: order_placed.send_waiting(
            conn, card_id, LATER, "почасовое чтение WebMIM", Deliver(fail=OSError("down")), FakeVk(fail=OSError("down"))))
    assert tail == "; кабинет MIM: вписано 1"


def test_hourly_a_failure_is_a_part_of_the_line(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    client = cabinet_of(Cabinet(listing=403))()
    with client:
        assert "не видит заказов кабинета" in webmim_cabinet.safe_hourly(conn, client, "prod", LATER, None)
    assert orders.get_card(conn, mine)["transfer_number"] == ""


# ---- the panel ---------------------------------------------------------------------------------------------------------------------------

PANEL_NOW = "2026-10-07T15:00:00"


def test_the_window_stays_after_the_press_and_asks_the_cabinet(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    box = SecretBox(Fernet.generate_key())
    fake = FakeWebmim()
    fake.cabinet = []
    app = make_app(tmp_path, SECRET_BOX=box, WEBMIM_HTTP=fake)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, PANEL_NOW)
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod", "login": "api-bot"}, PANEL_NOW, "owner")
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-SECRET-1", PANEL_NOW, "owner")
    conn.commit()
    mine = card(conn, delivery_date="2026-10-08")
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "owner", 0, "tok"
        session["pw"] = shared_mark_of(client.application)
    response = post(client, "/orders/%d/webmim/create" % mine, "tok")
    assert response.status_code == 302 and response.headers["Location"].endswith("/orders/%d?mim=1#supplier" % mine)
    html = client.get("/orders/%d?mim=1" % mine).get_data(as_text=True)
    assert "Сверка с кабинетом MIM" in html and 'data-mim-cabinet="/orders/%d/webmim/cabinet"' % mine in html
    assert "pw-SECRET-1" not in html
    number = orders.get_card(conn, mine)["webmim_number"]
    fake.cabinet = [row(int(number))]
    answer = client.post("/orders/%d/webmim/cabinet" % mine, data={"csrf": "tok"}).get_json()
    assert answer["state"] == "found" and answer["invoice"] == INVOICE and answer["result"] == "written"
    assert "pw-SECRET-1" not in json.dumps(answer) and fake.sessions == 0
    assert "Сверка с кабинетом MIM" not in client.get("/orders/%d" % mine).get_data(as_text=True)       # once: only right after the press
