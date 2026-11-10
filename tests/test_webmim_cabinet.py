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


def placed_order(tmp_path, cabinet=None, server="prod", login="api-bot", fail=None):
    """A placed order on the live server whose messages to the client (Avito) and the point (VK) need the transfer number; on_placed done."""
    conn = db(tmp_path, server=server)
    if login != "api-bot":
        connections.set_config(conn, "webmim_orders", 0, {"server": server, "login": login}, NOW, "owner")
    mine = with_vk(conn, ready(conn))
    waits_for_the_number(conn)
    fake = FakeWebmim()
    fake.cabinet = cabinet
    fake.fail = {key: list(actions) for key, actions in (fail or {}).items()}
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
    window = Cabinet([row(5001)])
    again, _, _ = poll(conn, mine, window, deliver=deliver, vk=vk)                                         # the window, then a second tab
    assert again["state"] == "found" and again["final"] and len(deliver.sent) == 1 and len(vk.sent) == 1
    assert again["text"] == "Номер перемещения %s вписан" % INVOICE and again["transfer"] == INVOICE       # the owner 2026-11-10: the number
    assert again["lines"] == ["Сообщения «заказ оформлен» отправлены: 2."] and window.calls == []         # is in: never a login for it
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
    said = ["Сообщения «заказ оформлен» отправлены: 2."]               # 2026-11-10: what became of the messages (read only), not the send's lines
    assert tab["state"] == "found" and tab["lines"] == said and len(cabinet.calls) == calls
    later, _, _ = poll(conn, mine, cabinet, "2026-10-23T12:02:00", deliver, vk)     # the number is in: the answer found, no login
    assert later["state"] == "found" and later["lines"] == said and len(cabinet.calls) == calls
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


def test_the_sums_differ_the_number_is_written_and_nothing_is_sent_but_a_yellow_record(tmp_path):
    """Review 09.11: a row of another sum may be another order — the number goes in, the messages wait for a person."""
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    result, deliver, vk = poll(conn, mine, Cabinet([row(5001, total=25000)]))
    assert result["result"] == "written" and result["lines"] == [webmim_cabinet.SUM_DIFFERS % ("25000", "24000", INVOICE)] and not deliver.sent and not vk.sent
    assert orders.get_card(conn, mine)["transfer_number"] == INVOICE
    assert any(level == "yellow" and "не сходится" in text and INVOICE in text for text, level in events(conn, mine))
    assert {t["status"] for t in message_tasks(conn, mine)} != {"done"}


def test_the_sums_differ_at_the_press_the_number_is_not_written_before_the_messages(tmp_path):
    conn, mine, fake, deliver, vk, lines = placed_order(tmp_path, cabinet=[row(5001, total=25000)])
    assert orders.get_card(conn, mine)["transfer_number"] == "" and not deliver.sent and not vk.sent


def test_a_number_typed_by_a_person_is_kept_and_nothing_is_sent(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    orders.update_card(conn, mine, {"transfer_number": "26R-1000001"}, NOW, "owner")
    cabinet = Cabinet([row(5001)])                   # the owner 2026-11-10: a number in the card — the window never asks the cabinet (before:
    result, deliver, vk = poll(conn, mine, cabinet)  # one login, «differs» and a yellow record; a typed number is checked by after_save)
    assert result["state"] == "found" and result["final"] and result["transfer"] == "26R-1000001" and not deliver.sent and not vk.sent
    assert result["text"] == "Номер перемещения 26R-1000001 вписан" and cabinet.calls == []
    assert result["lines"] == ["Сообщения «заказ оформлен» ждут в окне «Сообщения по заказу»: 2."]
    assert orders.get_card(conn, mine)["transfer_number"] == "26R-1000001"


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
    assert result["state"] == "error" and result["final"] is final and not deliver.sent
    if final:                                                            # the login never sees the list: said as it is, nothing to press
        assert words in result["text"] and not result.get("retry")
    else:                                                                # the owner 2026-11-10: a plain line and «Проверить ещё раз»; why — in the journal
        assert result["retry"] is True and result["text"] == webmim_cabinet.UNREACHABLE and words in result["why"]
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


# ---- «Проверить ещё раз» (the owner 2026-11-10) -------------------------------------------------------------------------------------------

BUSY_LOGIN = (429, {}, b"{}")                                            # the cabinet lets one session in


def logins(cabinet):
    return cabinet.calls.count(("POST", "/api/v3/auth/login"))


def test_a_failed_ask_can_be_asked_again_but_not_within_the_gap_and_the_number_is_written_once(tmp_path, monkeypatch):
    monkeypatch.setattr("tires_sync.reliability.default_sleep", lambda _s: None)
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    deliver, vk = Deliver(), FakeVk()
    cabinet = Cabinet([row(5001)], login=BUSY_LOGIN)
    failed, _, _ = poll(conn, mine, cabinet, "2026-10-23T12:01:00", deliver, vk)
    assert failed["state"] == "error" and not failed["final"] and failed["retry"] is True and "retry_in" not in failed
    assert failed["text"] == "Кабинет MIM занят или не отвечает. Номер подтянется сам при ежечасной проверке." and "429" in failed["why"]
    asked = logins(cabinet)
    cabinet.login = None                                                 # free again, but the button is pressed too early
    early, _, _ = poll(conn, mine, cabinet, "2026-10-23T12:01:01", deliver, vk)
    assert early["retry_in"] == 3 and not early["final"] and logins(cabinet) == asked and not deliver.sent
    assert orders.get_card(conn, mine)["transfer_number"] == ""
    done, _, _ = poll(conn, mine, cabinet, "2026-10-23T12:01:04", deliver, vk)
    assert done["state"] == "found" and done["result"] == "written" and done["final"] and "retry_in" not in done and not done.get("retry")
    assert logins(cabinet) == asked + 1 and len(deliver.sent) == 1 and len(vk.sent) == 1
    more, _, _ = poll(conn, mine, cabinet, "2026-10-23T12:01:30", deliver, vk)     # pressed again in another tab: no login, nothing sent
    assert more["text"] == "Номер перемещения %s вписан" % INVOICE and logins(cabinet) == asked + 1 and len(deliver.sent) == 1
    assert [t for t, _ in events(conn, mine)].count("Номер перемещения из кабинета MIM: %s (счёт от 23.10.2026)." % INVOICE) == 1


def test_the_page_stops_asking_after_a_failure_and_has_the_button():
    from pathlib import Path
    panel = Path(webmim_cabinet.__file__).parent / "panel"
    html = (panel / "templates" / "_order_supplier.html").read_text(encoding="utf-8")
    assert "data-mim-retry hidden>Проверить ещё раз</button>" in html
    js = (panel / "static" / "app.js").read_text(encoding="utf-8")
    block = js[js.index('document.querySelector("[data-mim-cabinet]")'):]
    block = block[:block.index("new MutationObserver")]
    assert webmim_cabinet.UNREACHABLE in block and "[data-mim-retry]" in block and "r.retry_in" in block
    assert block.count("setTimeout(ask") == 1                            # only while the cabinet answers and has no number yet
    assert block.index("if (r.retry || manual) { stop(") < block.index("setTimeout(ask")     # a failure: stop until the button
    assert ".catch(function () { stop(BUSY); })" in block                # no answer of the panel: the same, never a silent loop


# ---- hourly -----------------------------------------------------------------------------------------------------------------------------

def test_hourly_one_login_for_every_open_order_writes_and_sends_once(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    other = card(conn, point_id=point(conn, name="Парнас", webmim_address_id="78"))
    orders.update_card(conn, other, {"webmim_number": "7007"}, NOW, "owner")         # typed by hand: written; it has no message waiting
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
    calls = len(fake.calls)                                              # the window opened again (2026-11-10): the number is in, no request
    again = client.post("/orders/%d/webmim/cabinet" % mine, data={"csrf": "tok"}).get_json()
    assert again["final"] and again["text"] == "Номер перемещения %s вписан" % INVOICE and len(fake.calls) == calls
    assert "Сверка с кабинетом MIM" not in client.get("/orders/%d" % mine).get_data(as_text=True)       # once: only right after the press


# ---- the repeats after a refused read (the owner 2026-11-10: ~2, ~5, ~15 minutes, then hourly) ---------------------------------------

PRESS_REFUSED = {("POST", "/api/v3/auth/login"): ["timeout"]}            # the press's own read of the cabinet: the login times out


def at(minutes, seconds=0):
    return "2026-10-23T12:%02d:%02d" % (minutes, seconds)


def repeat(conn, cabinet, now, deliver, vk):
    return webmim_cabinet.retry_due(conn, cabinet_of(cabinet), now,
                                    lambda card_id: order_placed.send_waiting(conn, card_id, now, "повтор", deliver, vk))


def plan_of(conn, mine):
    return webmim_cabinet._state(conn, webmim_cabinet.RETRY % mine)


def test_a_refused_read_at_the_press_is_repeated_two_minutes_later_and_the_messages_go_once(tmp_path, monkeypatch):
    monkeypatch.setattr("tires_sync.reliability.default_sleep", lambda _s: None)
    conn, mine, fake, deliver, vk, lines = placed_order(tmp_path, cabinet=[row(5001)], fail=PRESS_REFUSED)
    assert orders.get_card(conn, mine)["transfer_number"] == "" and not deliver.sent and not vk.sent and fake.sessions == 0
    assert plan_of(conn, mine) == {"since": NOW, "tries": 0, "number": "5001"}
    cabinet = Cabinet([row(5002), row(5001)])
    assert repeat(conn, cabinet, at(1, 59), deliver, vk)["asked"] == 0 and cabinet.calls == []      # not yet: no login
    done = repeat(conn, cabinet, at(2), deliver, vk)
    assert done["asked"] == 1 and done["written"] == 1 and logins(cabinet) == 1 and cabinet.logouts == 1
    assert orders.get_card(conn, mine)["transfer_number"] == INVOICE and len(deliver.sent) == 1 and len(vk.sent) == 1
    assert plan_of(conn, mine) == {}
    assert repeat(conn, cabinet, at(5), deliver, vk)["asked"] == 0 and logins(cabinet) == 1        # done: never again
    client = cabinet_of(cabinet)()
    with client:                                                         # the hourly reading after it: nothing waits, nothing goes twice
        assert webmim_cabinet.safe_hourly(conn, client, "prod", at(7), None) == ""
    assert logins(cabinet) == 1 and len(deliver.sent) == 1 and len(vk.sent) == 1


@pytest.mark.parametrize("by", ["person", "hourly"])
def test_a_number_in_before_the_repeat_costs_no_login_and_nothing_goes_twice(tmp_path, monkeypatch, by):
    monkeypatch.setattr("tires_sync.reliability.default_sleep", lambda _s: None)
    conn, mine, fake, deliver, vk, lines = placed_order(tmp_path, cabinet=[row(5001)], fail=PRESS_REFUSED)
    assert plan_of(conn, mine)["tries"] == 0
    if by == "person":
        orders.update_card(conn, mine, {"transfer_number": "26R-7777777"}, at(1), "owner")
    else:
        client = cabinet_of(Cabinet([row(5001)]))()
        with client:
            webmim_cabinet.safe_hourly(conn, client, "prod", at(1),
                                       lambda card_id: order_placed.send_waiting(conn, card_id, at(1), "почасовое чтение WebMIM", deliver, vk))
    sent = (len(deliver.sent), len(vk.sent))
    cabinet = Cabinet([row(5001)])
    done = repeat(conn, cabinet, at(2), deliver, vk)
    assert done["asked"] == 0 and done["dropped"] == 1 and cabinet.calls == [] and plan_of(conn, mine) == {}
    assert (len(deliver.sent), len(vk.sent)) == sent


@pytest.mark.parametrize("login, listing, logged_in", [(BUSY_LOGIN, 200, False),
                                                        ((200, {}, json.dumps({"result": False, "message": "Превышено количество подключений"}).encode()), 200, False),
                                                        (None, 429, True), (None, "net", True)])
def test_three_refused_repeats_are_one_grey_record_and_no_more_repeats(tmp_path, monkeypatch, login, listing, logged_in):
    monkeypatch.setattr("tires_sync.reliability.default_sleep", lambda _s: None)
    conn, mine, fake, deliver, vk, lines = placed_order(tmp_path, cabinet=[row(5001)], fail=PRESS_REFUSED)
    cabinet = Cabinet([row(5001)], login=login, listing=listing)
    for minute, tries in ((2, 1), (4, 1), (5, 2), (14, 2), (15, 3)):
        repeat(conn, cabinet, at(minute), deliver, vk)
        assert plan_of(conn, mine)["tries"] == tries and logins(cabinet) == tries
    assert cabinet.logouts == (3 if logged_in else 0)                   # every session made is closed
    grey = [t for t, level in events(conn, mine) if level == "grey" and t.startswith("Номер перемещения из кабинета MIM не прочитан")]
    assert grey.count(webmim_cabinet.GAVE_UP) == 1 and len(grey) <= 3   # the press's failure, the repeats' one (once), the end
    for minute in (16, 30, 59):
        assert repeat(conn, cabinet, at(minute), deliver, vk)["asked"] == 0
    poll(conn, mine, cabinet, at(31))                                    # a later failed ask of the window plans nothing anew
    assert repeat(conn, cabinet, "2026-10-23T13:00:00", deliver, vk)["asked"] == 0 and plan_of(conn, mine)["done"] is True
    assert logins(cabinet) == 4 and not deliver.sent and orders.get_card(conn, mine)["transfer_number"] == ""
    assert [t for t, _ in events(conn, mine)].count(webmim_cabinet.GAVE_UP) == 1


def test_the_repeat_never_asks_within_the_gap_of_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr("tires_sync.reliability.default_sleep", lambda _s: None)
    conn, mine, fake, deliver, vk, lines = placed_order(tmp_path, cabinet=[row(5001)], fail=PRESS_REFUSED)
    webmim_cabinet._put(conn, webmim_cabinet.LAST, {"at": at(2)})       # the window has just asked (any order)
    conn.commit()
    cabinet = Cabinet([row(5001)])
    assert repeat(conn, cabinet, at(2, webmim_cabinet.GAP - 1), deliver, vk)["asked"] == 0 and cabinet.calls == []
    assert plan_of(conn, mine)["tries"] == 0                             # not counted: the next minute asks
    assert repeat(conn, cabinet, at(2, webmim_cabinet.GAP), deliver, vk)["written"] == 1 and logins(cabinet) == 1


def test_a_list_the_login_never_sees_is_not_repeated(tmp_path, monkeypatch):
    monkeypatch.setattr("tires_sync.reliability.default_sleep", lambda _s: None)
    conn, mine, fake, deliver, vk, lines = placed_order(tmp_path, cabinet=[row(5001)], fail=PRESS_REFUSED)
    cabinet = Cabinet([row(5001)], listing=403)
    repeat(conn, cabinet, at(2), deliver, vk)
    assert plan_of(conn, mine) == {} and cabinet.logouts == 1
    assert repeat(conn, cabinet, at(5), deliver, vk)["asked"] == 0 and logins(cabinet) == 1


def test_a_wrong_password_at_the_press_plans_no_repeat(tmp_path):
    conn, mine, *_ = placed_order(tmp_path, cabinet=[])
    poll(conn, mine, Cabinet(login=(200, {}, json.dumps({"result": False, "message": "bad"}).encode())))
    assert plan_of(conn, mine) == {}
    poll(conn, mine, Cabinet(login=BUSY_LOGIN), "2026-10-23T12:06:00")   # the window refused for the limit: repeats planned from then
    assert plan_of(conn, mine) == {"since": "2026-10-23T12:06:00", "tries": 0, "number": "5001"}


def test_the_minute_of_the_chats_runs_the_repeats_and_its_failure_is_only_a_warning(tmp_path, monkeypatch, capsys):
    from tires_sync import cli
    conn, mine, *_ = placed_order(tmp_path, cabinet=[row(5001)], fail=PRESS_REFUSED)
    box = SecretBox(Fernet.generate_key())
    asked = []
    monkeypatch.setattr(webmim_cabinet, "retry_due", lambda c, client_of, now, send: asked.append((now, send is not None)) or
                        {"asked": 1, "written": 1, "dropped": 0, "gave_up": 0})
    cli._mim_retry_look(conn, box, at(2))
    assert asked == [(at(2), True)] and "кабинет MIM, повтор: заказов 1, вписано 1" in capsys.readouterr().out

    def boom(*_a):
        raise RuntimeError("AUTH-SECRET")
    monkeypatch.setattr(webmim_cabinet, "retry_due", boom)
    cli._mim_retry_look(conn, box, at(3))                                # never raises into the reading of the chats
    err = capsys.readouterr().err
    assert "повтор чтения кабинета MIM не выполнен (RuntimeError)" in err and "AUTH-SECRET" not in err
