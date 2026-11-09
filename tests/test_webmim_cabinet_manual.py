"""A WebMIM number typed by a person (the partner places orders by hand in MIM's cabinet, the owner 2026-11-09): the order window's save
reads the cabinet at once (webmim_cabinet.after_save), the «Номер перемещения» goes in, and the messages «заказ оформлен» the rules of the
stage made, waiting for it, go by themselves, once («слать всем»: not only the orders of the button). Fakes only: no request leaves the tests."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from panel_helpers import make_app, post, shared_mark_of
from test_order_messages import FakeVk, with_vk
from test_order_placed import Deliver, ready
from test_roles import sign_in
from test_webmim_cabinet import INVOICE, LATER, Cabinet, cabinet_of, events, message_tasks, row, waits_for_the_number
from test_webmim_create import NOW, card, db, point
from tires_sync import (connections, order_placed, order_rules as rules, order_templates as templates, orders, store, users,
                        webmim_cabinet, webmim_orders)
from tires_sync.secretbox import SecretBox

WRITTEN = "Номер перемещения из кабинета MIM: %s (счёт от 23.10.2026)." % INVOICE


class Timed(Cabinet):
    """The cabinet, with the seconds every request was given."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeouts = []

    def __call__(self, method, url, headers, body, timeout=None):
        self.timeouts.append(timeout)
        return super().__call__(method, url, headers, body, timeout)


def stage_rules(conn):
    """The rules of «Заказ оформлен»: the client's and the point's message, as the owner set them up."""
    placed = orders._placed_stage(conn)["id"]
    for name in (order_placed.CLIENT_TEMPLATE, order_placed.POINT_TEMPLATE):
        template = next(t for t in templates.list_templates(conn) if t["name"] == name)
        rules.add_rule(conn, placed, "enter", 0, "message", template["id"], "", "ask", None)
    conn.commit()
    return placed


def by_hand(conn, number="5001", move=True):
    """An order placed by hand in the cabinet: no press of the button; its WebMIM number typed, moved to «Заказ оформлен»."""
    mine = with_vk(conn, ready(conn))
    waits_for_the_number(conn)
    placed = stage_rules(conn)
    if number:
        orders.update_card(conn, mine, {"webmim_number": number}, NOW, "owner")
    if move:
        assert orders.move_card(conn, mine, placed, NOW, "owner")
    return mine


def manual(tmp_path, server="prod", **kwargs):
    conn = db(tmp_path, server=server)
    return conn, by_hand(conn, **kwargs)


def save(conn, mine, cabinet, now=LATER, deliver=None, vk=None):
    deliver, vk = deliver if deliver is not None else Deliver(), vk if vk is not None else FakeVk()
    result = webmim_cabinet.after_save(conn, mine, cabinet_of(cabinet), now,
                                       lambda card_id: order_placed.send_waiting(conn, card_id, now, "owner", deliver, vk))
    return result, deliver, vk


def logins(cabinet):
    return cabinet.calls.count(("POST", "/api/v3/auth/login"))


# ---- the reading of the save ---------------------------------------------------------------------------------------------------------------

def test_a_number_typed_by_hand_takes_the_transfer_number_and_both_messages_go_once(tmp_path):
    conn, mine = manual(tmp_path)
    assert {t["status"] for t in message_tasks(conn, mine)} == {"error"}                  # the rules' tasks wait for the transfer number
    cabinet = Timed([row(50011), row(5001)])                                              # 50011 is not 5001: the number is exact
    (result, lines), deliver, vk = save(conn, mine, cabinet)
    assert result == "written" and orders.get_card(conn, mine)["transfer_number"] == INVOICE
    assert len(deliver.sent) == 1 and len(vk.sent) == 1 and "перемещение " + INVOICE in deliver.sent[0][1]
    assert len(lines) == 2 and lines[0].startswith("Клиенту отправлено") and lines[1].startswith("Точке «")
    assert {t["status"] for t in message_tasks(conn, mine)} == {"done"}
    assert (WRITTEN, "grey") in events(conn, mine)
    assert logins(cabinet) == 1 == cabinet.logouts and cabinet.calls.count(("GET", "/api/v3/orders")) == 1
    assert set(cabinet.timeouts) == {webmim_orders.CABINET_QUICK}                      # the login, the list and the logout: all quick


def test_a_second_save_the_hourly_reading_and_a_second_worker_send_nothing_again(tmp_path):
    conn, mine = manual(tmp_path)
    deliver, vk = Deliver(), FakeVk()
    cabinet = Cabinet([row(5001)])
    (result, _), _, _ = save(conn, mine, cabinet, deliver=deliver, vk=vk)
    assert result == "written" and len(deliver.sent) == 1 and len(vk.sent) == 1
    (again, lines), _, _ = save(conn, mine, cabinet, now="2026-10-23T12:10:00", deliver=deliver, vk=vk)     # the number is in: no login
    assert (again, lines) == ("off", []) and logins(cabinet) == 1
    client = cabinet_of(cabinet)()
    with client:
        assert webmim_cabinet.safe_hourly(conn, client, "prod", "2026-10-23T13:00:00", lambda card_id: order_placed.send_waiting(
            conn, card_id, "2026-10-23T13:00:00", "почасовое чтение WebMIM", deliver, vk)) == ""
    assert webmim_cabinet.take(conn, mine, webmim_cabinet.info(row(5001)), "2026-10-23T13:01:00", lambda card_id: order_placed.send_waiting(
        conn, card_id, "2026-10-23T13:01:00", "owner", deliver, vk)) == ("same", [])
    assert order_placed.send_waiting(conn, mine, "2026-10-23T13:02:00", "owner", deliver, vk) == []
    assert len(deliver.sent) == 1 and len(vk.sent) == 1 and logins(cabinet) == 1
    assert [t for t, _ in events(conn, mine)].count(WRITTEN) == 1


def test_the_hourly_reading_sends_the_messages_of_an_order_placed_by_hand_once(tmp_path):
    conn, mine = manual(tmp_path)
    deliver, vk = Deliver(), FakeVk()
    cabinet = Cabinet([row(5001)])
    for at in ("2026-10-23T13:00:00", "2026-10-23T14:00:00"):
        client = cabinet_of(cabinet)()
        with client:
            webmim_cabinet.safe_hourly(conn, client, "prod", at, lambda card_id: order_placed.send_waiting(
                conn, card_id, at, "почасовое чтение WebMIM", deliver, vk))
    assert orders.get_card(conn, mine)["transfer_number"] == INVOICE
    assert len(deliver.sent) == 1 and len(vk.sent) == 1 and logins(cabinet) == 1           # the second hour: nothing waits, no login


def test_a_transfer_number_typed_by_a_person_is_never_overwritten(tmp_path):
    conn, mine = manual(tmp_path)
    orders.update_card(conn, mine, {"transfer_number": "26R-1111111"}, NOW, "owner")
    cabinet = Cabinet([row(5001)])
    (result, lines), deliver, vk = save(conn, mine, cabinet)
    assert (result, lines) == ("off", []) and not cabinet.calls                            # in already: no login for it
    assert orders.get_card(conn, mine)["transfer_number"] == "26R-1111111"
    # typed while the cabinet was being read: kept, a yellow record, nothing sent
    seen = webmim_cabinet.info(row(5001))
    assert webmim_cabinet.take(conn, mine, seen, LATER, lambda card_id: order_placed.send_waiting(conn, card_id, LATER, "owner", deliver, vk)) == ("differs", [])
    assert orders.get_card(conn, mine)["transfer_number"] == "26R-1111111" and not deliver.sent and not vk.sent
    assert any(level == "yellow" and "26R-1111111" in text and INVOICE in text for text, level in events(conn, mine))


def test_the_sums_differ_the_number_is_written_and_nothing_is_sent_but_a_yellow_record(tmp_path):
    conn, mine = manual(tmp_path)
    (result, lines), deliver, vk = save(conn, mine, Cabinet([row(5001, total=30000)]))
    assert result == "written" and lines == [] and orders.get_card(conn, mine)["transfer_number"] == INVOICE
    assert not deliver.sent and not vk.sent
    assert (webmim_cabinet.SUM_DIFFERS % ("30000", "24000", INVOICE), "yellow") in events(conn, mine)


@pytest.mark.parametrize("cabinet, words", [
    (Cabinet(listing="net"), "кабинет недоступен (OSError)"),
    (Cabinet(listing=403), "не видит заказов кабинета"),
    (Cabinet(login=(200, {}, b'{"result": false, "message": "x"}')), "вход в кабинет не удался"),
])
def test_mim_down_is_a_grey_record_once_and_nothing_changes(tmp_path, cabinet, words):
    conn, mine = manual(tmp_path)
    (result, lines), deliver, vk = save(conn, mine, cabinet)
    assert (result, lines) == ("failed", []) and orders.get_card(conn, mine)["transfer_number"] == ""
    save(conn, mine, cabinet, now="2026-10-23T12:10:00")                                   # a second save: the same record, not twice
    grey = [text for text, level in events(conn, mine) if level == "grey" and words in text]
    assert len(grey) == 1 and "почасового чтения" in grey[0]
    assert not deliver.sent and not vk.sent and {t["status"] for t in message_tasks(conn, mine)} == {"error"}
    assert cabinet.logouts == logins(cabinet) - (0 if cabinet.login is None else logins(cabinet))     # every session made is closed
    assert "pw-SECRET-1" not in str(events(conn, mine)) and "AUTH-SECRET" not in str(events(conn, mine))


def test_not_in_the_cabinet_yet_is_a_grey_record_once_the_hourly_reading_takes_it(tmp_path):
    conn, mine = manual(tmp_path)
    cabinet = Cabinet([row(5001, invoice=None)])
    assert save(conn, mine, cabinet)[0] == ("wait", [])
    cabinet.rows = []
    assert save(conn, mine, cabinet, now="2026-10-23T12:10:00")[0] == ("wait", [])
    assert [t for t, _ in events(conn, mine)].count(webmim_cabinet.LATER % "5001") == 1
    cabinet.rows = [row(5001)]
    deliver, vk = Deliver(), FakeVk()
    client = cabinet_of(cabinet)()
    with client:
        webmim_cabinet.safe_hourly(conn, client, "prod", "2026-10-23T13:00:00", lambda card_id: order_placed.send_waiting(
            conn, card_id, "2026-10-23T13:00:00", "почасовое чтение WebMIM", deliver, vk))
    assert orders.get_card(conn, mine)["transfer_number"] == INVOICE and len(deliver.sent) == 1 and len(vk.sent) == 1


def test_two_saves_within_the_gap_ask_the_cabinet_once(tmp_path):
    conn, mine = manual(tmp_path)
    other = by_hand(conn, number="5002")
    cabinet = Cabinet([])
    assert save(conn, mine, cabinet, now="2026-10-23T12:05:00")[0] == ("wait", [])
    assert save(conn, other, cabinet, now="2026-10-23T12:05:02")[0] == ("busy", [])       # sessions are limited: the hourly reading
    assert logins(cabinet) == 1 and (webmim_cabinet.LATER % "5002", "grey") in events(conn, other)


def test_the_test_server_does_nothing(tmp_path):
    conn, mine = manual(tmp_path, server="test")
    cabinet = Cabinet([row(5001)])
    assert save(conn, mine, cabinet)[0] == ("off", []) and not cabinet.calls
    assert orders.get_card(conn, mine)["transfer_number"] == ""


def test_an_empty_login_does_nothing(tmp_path):
    conn, mine = manual(tmp_path)
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod", "login": ""}, NOW, "owner")
    conn.commit()
    cabinet = Cabinet([row(5001)])
    assert save(conn, mine, cabinet)[0] == ("off", []) and not cabinet.calls


def test_a_number_of_two_orders_or_a_closed_order_is_never_read(tmp_path):
    conn, mine = manual(tmp_path)
    twin = card(conn, point_id=point(conn, name="Парнас"))
    orders.update_card(conn, twin, {"webmim_number": "5001"}, NOW, "owner")
    cabinet = Cabinet([row(5001)])
    assert save(conn, mine, cabinet)[0] == ("off", []) and not cabinet.calls
    orders.update_card(conn, twin, {"webmim_number": ""}, NOW, "owner")
    cancel = next(s["id"] for s in orders.list_stages(conn) if s["kind"] != "open")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (cancel, mine))
    conn.commit()
    assert save(conn, mine, cabinet)[0] == ("off", []) and not cabinet.calls


# ---- the order window ----------------------------------------------------------------------------------------------------------------------

PANEL_NOW = "2026-10-07T15:00:00"


def panel(tmp_path, monkeypatch, cabinet):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    box = SecretBox(Fernet.generate_key())
    app = make_app(tmp_path, SECRET_BOX=box, WEBMIM_HTTP=cabinet)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, PANEL_NOW)
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod", "login": "api-bot"}, PANEL_NOW, "owner")
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-SECRET-1", PANEL_NOW, "owner")
    conn.commit()
    mine = by_hand(conn, number="", move=False)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "owner", 0, "tok"
        session["pw"] = shared_mark_of(client.application)
    return app, conn, mine, client


def test_the_window_save_of_the_number_and_the_move_takes_the_transfer_number_and_tries_the_messages_once(tmp_path, monkeypatch):
    cabinet = Cabinet([row(5001)])
    app, conn, mine, client = panel(tmp_path, monkeypatch, cabinet)
    placed = orders._placed_stage(conn)["id"]
    response = post(client, "/orders/%d/save" % mine, "tok", webmim_number="5001", stage_id=str(placed))
    assert response.status_code == 302
    got = orders.get_card(conn, mine)
    assert got["webmim_number"] == "5001" and got["transfer_number"] == INVOICE and got["stage"]["id"] == placed
    assert logins(cabinet) == 1 == cabinet.logouts
    # the send path ran once for each (no Avito keys, no VK key in this panel: each says so once, and waits for a person)
    texts = [text for text, _ in events(conn, mine)]
    assert sum("не отправлено" in t and order_placed.CLIENT_TEMPLATE in t for t in texts) == 1
    assert sum("не отправлено" in t and order_placed.POINT_TEMPLATE in t for t in texts) == 1
    page = client.get("/orders/%d" % mine).get_data(as_text=True)
    assert "Номер перемещения взят из кабинета MIM: %s." % INVOICE in page and "pw-SECRET-1" not in page and "AUTH-SECRET" not in page
    post(client, "/orders/%d/save" % mine, "tok", webmim_number="5001", client_name="Иван")    # the number unchanged: no login
    assert logins(cabinet) == 1


def test_the_window_save_never_fails_for_mim(tmp_path, monkeypatch):
    cabinet = Cabinet(listing="net")
    app, conn, mine, client = panel(tmp_path, monkeypatch, cabinet)
    response = post(client, "/orders/%d/save" % mine, "tok", webmim_number="5001")
    assert response.status_code == 302 and orders.get_card(conn, mine)["webmim_number"] == "5001"
    assert orders.get_card(conn, mine)["transfer_number"] == ""
    assert any(level == "grey" and "кабинет недоступен" in text for text, level in events(conn, mine))
    monkeypatch.setattr(webmim_cabinet, "after_save", lambda *a: 1 / 0)                     # a failure of the panel itself
    response = post(client, "/orders/%d/save" % mine, "tok", webmim_number="5003")
    assert response.status_code == 302 and orders.get_card(conn, mine)["webmim_number"] == "5003"


def test_a_point_cannot_save_the_order_window(tmp_path, monkeypatch):
    cabinet = Cabinet([row(5001)])
    app, conn, mine, client = panel(tmp_path, monkeypatch, cabinet)
    pid = orders.get_card(conn, mine)["point_id"]
    users.add_user(conn, "gamma", "пароль-для-точки-1", "2026-10-15T12:00:00", role="point", point_id=pid)
    sign_in(client, conn, "gamma")
    assert client.post("/orders/%d/save" % mine, data={"csrf": "tok", "webmim_number": "5001"}).status_code == 403
    assert orders.get_card(conn, mine)["webmim_number"] == "" and not cabinet.calls
