"""«Оформить в WebMIM» (webmim_create): an order of the panel made in WebMIM by a button — the checks before, no doubles, a part made, the test server only,
the rights, and a test order kept away from the live reconciliation. A fake WebMIM only: no request leaves the tests."""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
from datetime import date

import pytest
from cryptography.fernet import Fernet

from panel_helpers import make_app, post, shared_mark_of
from order_fixtures import free_phone
from tires_sync import connections, order_points as points, order_report, orders, store, users, webmim_create, webmim_match, webmim_orders
from tires_sync.secretbox import SecretBox

NOW = "2026-10-23T12:00:00"
TODAY = date(2026, 10, 23)
TERRITORY = "85858701785"
PASSWORD = "длинный-пароль-1"


class FakeWebmim:
    """WebMIM's orders API as far as the button uses it. `fail[(method, path)]` is a list of what the next such calls do instead:
    "timeout" (nothing made, the network failed), "made-then-timeout" (made, the answer lost), "500", or a 422 text."""

    def __init__(self, payment_types=None, terms=None, shipments=None, addresses=None):
        self.payment_types = {"3": "безнал", "2": "физ.лицо"} if payment_types is None else payment_types
        self.terms = {"1": "Предоплата", "2": "Отсрочка"} if terms is None else terms
        self.shipments = {"1": "Доставка", "2": "Самовывоз"} if shipments is None else shipments
        self.addresses = [{"id": "77", "address": "СПб, Гамма"}, {"id": "78", "address": "СПб, Парнас"}] if addresses is None else addresses
        self.orders, self.items, self.fail, self.calls = [], {}, {}, []

    def add_order(self, external, created="23.10.26 11:00:00"):
        order = {"id": 1000 + len(self.orders) + 1, "number": 5000 + len(self.orders) + 1, "externalNumber": external, "createDate": created,
                 "statusCode": "0", "statusDescription": "создано"}
        self.orders.append(order)
        self.items[str(order["id"])] = []
        return order

    def __call__(self, method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        path, query = parts.path, dict(urllib.parse.parse_qsl(parts.query))
        payload = json.loads(body.decode("utf-8")) if body else None
        self.calls.append((method, path, payload))
        key = (method, "/api/v2/orders/{id}/items" if path.startswith("/api/v2/orders/") and path.endswith("/items") else path)
        action = self.fail.get(key, []).pop(0) if self.fail.get(key) else None
        if action == "timeout":
            raise OSError("timed out")
        if action == "500":
            return 500, {}, b"{}"
        if action and action != "made-then-timeout":
            return 422, {}, json.dumps({"field": "checkMessage", "message": action}, ensure_ascii=False).encode("utf-8")
        status, data, extra = self._answer(method, path, query, payload)
        if action == "made-then-timeout":
            raise OSError("timed out")
        return status, extra, json.dumps(data, ensure_ascii=False).encode("utf-8")

    def _answer(self, method, path, query, payload):
        if path == "/api/v2/token":
            return 200, {"accessToken": "TOKEN-1", "tokenType": "bearer"}, {}
        if path == "/api/v2/account/payment-types":
            return 200, self.payment_types, {}
        if path == "/api/v2/account/payment-terms":
            return 200, self.terms, {}
        if path == "/api/v2/account/shipment-types":
            return 200, self.shipments, {}
        if path == "/api/v2/addresses":
            return 200, self.addresses, {}
        if path == "/api/v2/orders" and method == "GET":
            page, per = int(query.get("page", 1)), int(query.get("per-page", 50))
            rows = list(reversed(self.orders))                                        # the newest first
            pages = max(1, -(-len(rows) // per))
            return 200, rows[(page - 1) * per: page * per], {"X-Pagination-Page-Count": str(pages)}
        if path == "/api/v2/orders" and method == "POST":
            order = dict(self.add_order(payload.get("externalNumber"), "23.10.26 12:00:00"), **{k: v for k, v in payload.items()})
            self.orders[-1] = order
            return 201, order, {}
        order_id = path.split("/")[4]
        if method == "GET":
            return 200, self.items.get(order_id, []), {"X-Pagination-Page-Count": "1"}
        rows = self.items.setdefault(order_id, [])
        for row in rows:
            if row["goodsId"] == payload["goodsId"] and row["territoryId"] == payload["territoryId"]:
                row["quantity"] = int(row["quantity"]) + payload["quantity"]           # WebMIM: the same position again adds to it
                return 201, row, {}
        row = dict(payload, price=4000, sum=4000 * payload["quantity"], goodsName="Шина " + payload["goodsId"], orderId=int(order_id))
        rows.append(row)
        return 201, row, {}

    def posts(self, path_end=""):
        return [c for c in self.calls if c[0] == "POST" and c[1] != "/api/v2/token" and c[1].endswith(path_end)]

    def held(self, order_id):
        return {r["goodsId"]: r["quantity"] for r in self.items.get(str(order_id), [])}


def db(tmp_path, server="test"):
    conn = store.connect(tmp_path / "t.db")
    orders.ensure_pipeline(conn, NOW)
    connections.set_config(conn, "webmim_orders", 0, {"server": server, "login": "api-bot"}, NOW, "owner")
    conn.commit()
    return conn


def point(conn, **fields):
    data = dict({"name": "Гамма", "manager_name": "Алексей", "manager_phone": "8 911 222-33-44", "channel": "vk", "webmim_address_id": "77",
                 "default_time": "10:00-18:00"}, **fields)
    return points.save_point(conn, data)


def card(conn, goods=(("111", 4), ("222", 2)), territory=TERRITORY, brand="b", point_id="auto", **fields):
    if point_id == "auto":
        found = conn.execute("SELECT id FROM point WHERE name = 'Гамма'").fetchone()
        point_id = found[0] if found else point(conn)
    data = dict({"client_name": "Иван", "client_phone": free_phone(conn, "+7 (911) 111-22-33"), "delivery_date": "2026-10-24", "point_id": point_id, "tags": "тест"}, **fields)
    if point_id is None:
        data.pop("point_id")
    card_id = orders.create_card(conn, data, NOW, "owner")
    for goods_id, qty in goods:
        orders.add_item(conn, card_id, goods_id=goods_id, name="Шина " + goods_id, brand_key=brand, season="summer", diameter=16, territory=territory,
                        qty=qty, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="owner")
    conn.commit()
    return card_id


def client_of(fake):
    from tires_sync import webmim_orders
    return webmim_orders.OrdersClient("https://mimtest.example", "api-bot", "pw-SECRET-1", http=fake)


def place(conn, card_id, fake, server="test", now=NOW, today=TODAY):
    return webmim_create.place(conn, card_id, client_of(fake), server, now, today, "owner", sleep=lambda _s: None)


def events(conn, card_id):
    return [e["text"] for e in orders.list_events(conn, card_id)]


# ---- the whole way ---------------------------------------------------------------------------------------------------------------------

def test_the_order_and_its_positions_are_made_and_written_to_the_order(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, goods=(("111", 4), ("222", 2), ("111", 1)))
    stage_before = orders._row(conn, mine)["stage_id"]
    fake = FakeWebmim()
    result = place(conn, mine, fake)
    assert result["state"] == "done" and result["ok"]
    sent = fake.posts("/api/v2/orders")[0][2]
    # the owner 2026-10-29, as he fills the form by hand: «безнал», «Отсрочка», «Доставка», contact «Админ», the point's phone and time,
    # «Грузим на ОТХ»; no data of the client
    assert sent == {"orderDate": "24.10.26", "paymentType": 3, "paymentTerms": 2, "shipmentType": 1, "deliveryAddressId": "77",
                    "deliveryTime": "10:00-18:00", "deliveryPerson": "Админ", "deliveryPhone": "+7(911)222-33-44", "externalNumber": str(mine),
                    "noteForSupplier": "Грузим на ОТХ", "noteForSelf": "Tires Sync, заказ №%d" % mine}
    assert [c[2] for c in fake.posts("/items")] == [{"goodsId": "111", "territoryId": TERRITORY, "stockType": 0, "quantity": 5},
                                                     {"goodsId": "222", "territoryId": TERRITORY, "stockType": 0, "quantity": 2}]
    row = orders._row(conn, mine)
    assert (row["webmim_id"], row["webmim_number"], row["webmim_status"]) == ("1001", "5001", "создано")
    assert orders.get_stage(conn, stage_before)["code"] == "new"
    assert orders.get_card(conn, mine)["stage"]["code"] == "placed"                    # the owner 2026-10-23: placed in WebMIM — «Заказ оформлен»
    assert not [c for c in fake.calls if "approve" in c[1]]
    assert all(c[1].startswith("/api/v2/") for c in fake.calls)
    text = " | ".join(events(conn, mine))
    assert "безнал (id 3)" in text and "Отсрочка (id 2)" in text and "№5001" in text and "тестовый" in text.lower()
    assert "безнал (id 3)" in result["text"] and "Отсрочка (id 2)" in result["text"]
    state = webmim_create.get_state(conn, mine)
    assert state["server"] == "test" and state["state"] == "done" and state["webmim_id"] == "1001"


def stage(conn, code):
    return next(st for st in orders.list_stages(conn) if st["code"] == code)


def put_on(conn, card_id, code):
    """The card on a stage without the checks of the move (the setup of a test)."""
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (stage(conn, code)["id"], card_id))
    conn.commit()


@pytest.mark.parametrize("code", ["new", "talk", "wait"])
def test_a_full_success_moves_an_earlier_order_to_placed_the_ordinary_way(tmp_path, code):
    conn = db(tmp_path)
    mine = card(conn)
    put_on(conn, mine, code)
    result = place(conn, mine, FakeWebmim())
    assert result["ok"] and orders.get_card(conn, mine)["stage"]["code"] == "placed"
    line = "Этап: «%s» → «Заказ оформлен»" % stage(conn, code)["name"]
    assert line in result["text"] and line in events(conn, mine)[0]                       # the move's own record, after the result's
    assert "Оформлен в WebMIM" in events(conn, mine)[1]


@pytest.mark.parametrize("code", ["placed", "moving", "at_point", "done", "cancel"])
def test_an_order_at_placed_or_later_or_closed_keeps_its_stage(tmp_path, code):
    conn = db(tmp_path)
    mine = card(conn)
    put_on(conn, mine, code)
    result = place(conn, mine, FakeWebmim())
    assert result["ok"] and orders.get_card(conn, mine)["stage"]["code"] == code and "Этап" not in result["text"]
    assert not [e for e in events(conn, mine) if e.startswith("Этап") or e.startswith("Перевод")]


def test_a_move_the_stage_refuses_leaves_the_order_and_says_why(tmp_path):
    """WebMIM needs no client name, «Заказ оформлен» does: the WebMIM order is made, the card stays where it was."""
    conn = db(tmp_path)
    mine = card(conn, client_name="")
    result = place(conn, mine, FakeWebmim())
    assert result["ok"] and result["state"] == "done" and orders._row(conn, mine)["webmim_id"] == "1001"
    assert orders.get_card(conn, mine)["stage"]["code"] == "new"
    assert "Этап не сменился (остался «Новый заказ»)" in result["stage_error"] and "имя клиента" in result["stage_error"]
    assert "Этап не сменился" not in result["text"]                                           # хвост З4: apart from the green result
    assert any(e.startswith("Перевод не выполнен") and "имя клиента" in e for e in events(conn, mine))


def test_a_crash_of_the_move_never_breaks_the_press(tmp_path, monkeypatch):
    conn = db(tmp_path)
    mine = card(conn)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(orders, "move_card", boom)
    result = place(conn, mine, FakeWebmim())
    assert result["ok"] and "Этап не сменился" in result["stage_error"] and orders.get_card(conn, mine)["stage"]["code"] == "new"
    assert webmim_create.get_state(conn, mine)["state"] == "done"


def test_a_part_made_keeps_the_stage(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders/{id}/items")] = ["timeout"]
    assert place(conn, mine, fake)["state"] == "partial"
    assert orders.get_card(conn, mine)["stage"]["code"] == "new"
    assert place(conn, mine, fake, now="2026-10-23T12:05:00")["state"] == "done"                 # «Досоздать»: now it is the first full success
    assert orders.get_card(conn, mine)["stage"]["code"] == "placed"


def test_the_test_server_journals_the_names_of_webmims_fields_never_their_values(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    original = fake._answer

    def answer(method, path, query, payload):
        status, data, extra = original(method, path, query, payload)
        if method == "POST" and path == "/api/v2/orders":
            data = dict(data, transferDoc={"number": "26R-922352", "date": "23.10.26"},
                        deliveryRequests=[{"id": 7, "trackNumber": "TRACK-SECRET"}])
        return status, data, extra
    fake._answer = answer
    place(conn, mine, fake)
    [line] = [e for e in orders.list_events(conn, mine) if e["text"].startswith("Поля ответа WebMIM")]
    assert line["level"] == "grey"
    for name in ("id", "number", "externalNumber", "transferDoc", "transferDoc.number", "deliveryRequests[].trackNumber"):
        assert name in line["text"].split(": ", 1)[1].rstrip(".").split(", ")
    assert "26R-922352" not in line["text"] and "TRACK-SECRET" not in line["text"] and "Иван" not in line["text"] and "1001" not in line["text"]


def test_the_live_server_journals_no_field_names(tmp_path, monkeypatch):
    conn = db(tmp_path, server="prod")
    mine = card(conn)
    monkeypatch.setattr(webmim_create, "CREATE_SERVERS", ("test", "prod"))
    place(conn, mine, FakeWebmim(), server="prod")
    assert not [t for t in events(conn, mine) if t.startswith("Поля ответа WebMIM")]


def test_the_order_time_goes_before_the_points_default(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, delivery_time="после 15:00")
    fake = FakeWebmim()
    place(conn, mine, fake)
    assert fake.posts("/api/v2/orders")[0][2]["deliveryTime"] == "после 15:00"


def test_a_single_payment_type_is_not_taken_unless_it_is_beznal(tmp_path):
    """The owner 2026-10-29: «безнал» only; the only type of the dictionary is no longer taken whatever its name."""
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim(payment_types={"44430634001": "Оплата со счёта"})
    result = place(conn, mine, fake)
    assert result["state"] == "refused" and "Оплата со счёта" in result["text"] and not fake.posts()
    fake = FakeWebmim(payment_types={"44430634001": "Безнал"})
    place(conn, mine, fake)
    assert fake.posts("/api/v2/orders")[0][2]["paymentType"] == 44430634001


def test_the_card_time_is_kept_and_the_points_time_is_taken_without_it(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, point_id=point(conn, name="Янино", default_time="09:00-17:00"))
    data = webmim_create.fields(conn, orders.get_card(conn, mine))
    assert (data["time"], data["person"], data["phone"], data["note"]) == ("09:00-17:00", "Админ", "+7(911)222-33-44", "Грузим на ОТХ")


@pytest.mark.parametrize("kind, value, words", [
    ("payment_types", {"2": "физ.лицо", "5": "наличные"}, "Вид оплаты"),
    ("payment_types", {"5": "наличные"}, "Вид оплаты"),
    ("terms", {"1": "Предоплата"}, "Признак оплаты"),
    ("terms", {"1": "Предоплата", "2": "Отсрочка платежа"}, "Признак оплаты"),
    ("shipments", {"2": "Самовывоз"}, "доставка"),
])
def test_a_dictionary_without_the_wanted_value_refuses_with_its_values(tmp_path, kind, value, words):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim(**{kind: value})
    result = place(conn, mine, fake)
    assert result["state"] == "refused" and not result["ok"] and words.lower() in result["text"].lower()
    assert all(name in result["text"] for name in value.values())                      # the values of the dictionary are listed: no guessing
    assert not fake.posts() and orders._row(conn, mine)["webmim_id"] == ""


def test_the_points_address_must_be_one_of_the_servers(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, point_id=point(conn, name="Мурино", webmim_address_id="999"))
    fake = FakeWebmim()
    result = place(conn, mine, fake)
    assert result["state"] == "refused" and "999" in result["text"] and "77" in result["text"] and "СПб, Гамма" in result["text"]
    assert not fake.posts()


def test_webmims_refusal_of_the_order_is_shown_as_it_is_and_a_new_press_tries_again(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders")] = ["Нельзя создавать заказ задним числом."]
    result = place(conn, mine, fake)
    assert result["state"] == "refused" and "Нельзя создавать заказ задним числом." in result["text"]
    assert orders._row(conn, mine)["webmim_id"] == "" and not fake.orders
    assert any("задним числом" in t for t in events(conn, mine))
    assert place(conn, mine, fake)["state"] == "done" and len(fake.orders) == 1


# ---- no doubles ------------------------------------------------------------------------------------------------------------------------

def test_an_order_with_a_webmim_number_already_is_refused_without_asking_webmim(tmp_path):
    conn = db(tmp_path)
    for fields in ({"webmim_number": "777"}, {"webmim_id": "888"}):
        mine = card(conn, **fields)
        fake = FakeWebmim()
        with pytest.raises(orders.OrderError, match="уже есть номер WebMIM"):
            place(conn, mine, fake)
        assert fake.calls == []


def test_a_done_order_is_not_made_again(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    place(conn, mine, fake)
    calls = len(fake.calls)
    with pytest.raises(orders.OrderError, match="уже оформлен"):
        place(conn, mine, fake)
    assert len(fake.calls) == calls and len(fake.orders) == 1


def test_a_second_press_while_the_first_runs_is_refused_and_a_stale_mark_is_not(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, updated_at) VALUES (?, 'test', 'running', ?, ?, 'partner1', ?)",
                 (mine, NOW, "2026-10-23T11:58:00", "2026-10-23T11:58:00"))
    conn.commit()
    fake = FakeWebmim()
    with pytest.raises(orders.OrderError, match="уже оформляется"):
        place(conn, mine, fake)
    assert fake.calls == []
    result = place(conn, mine, fake, now="2026-10-23T12:09:00")                          # 11 minutes later: a killed worker's mark
    assert result["state"] == "done" and len(fake.orders) == 1


def test_the_mark_is_taken_before_webmim_is_asked(tmp_path, monkeypatch):
    conn = db(tmp_path)
    mine = card(conn)
    seen = []
    fake = FakeWebmim()

    def spy(*args):
        if not seen:
            other = store.connect(tmp_path / "t.db")
            seen.append(other.execute("SELECT state FROM webmim_create WHERE order_id = ?", (mine,)).fetchone())
            other.close()
        return fake(*args)
    webmim_create.place(conn, mine, client_of(spy), "test", NOW, TODAY, "owner", sleep=lambda _s: None)
    assert seen == [("running",)]                                                      # another window sees «оформляется» at the first request
    assert webmim_create.get_state(conn, mine)["state"] == "done"


def test_a_lost_answer_of_the_order_is_found_by_our_number_and_not_made_twice(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders")] = ["made-then-timeout"]
    first = place(conn, mine, fake)
    assert first["state"] == "unknown" and "мог создаться" in first["text"] and orders._row(conn, mine)["webmim_id"] == ""
    second = place(conn, mine, fake, now="2026-10-23T12:01:00")
    assert second["state"] == "done" and len(fake.orders) == 1 and len(fake.posts("/api/v2/orders")) == 1
    assert orders._row(conn, mine)["webmim_id"] == "1001" and fake.held(1001) == {"111": 4, "222": 2}
    assert any("найден" in t.lower() for t in events(conn, mine))


@pytest.mark.parametrize("failure", ["timeout", "500"])
def test_a_lost_order_that_was_not_made_is_made_on_the_next_press(tmp_path, failure):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders")] = [failure]
    assert place(conn, mine, fake)["state"] == "unknown"
    assert place(conn, mine, fake, now="2026-10-23T12:01:00")["state"] == "done"
    assert len(fake.orders) == 1 and len(fake.posts("/api/v2/orders")) == 2


def test_a_webmim_order_with_our_number_but_not_ours_stops_the_button(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.add_order(str(mine), "22.10.26 09:00:00")                                    # made by a copy of the panel, a restored base, a person
    result = place(conn, mine, fake)
    assert result["state"] == "refused" and "сверьте" in result["text"] and "5001" in result["text"]
    assert not fake.posts() and orders._row(conn, mine)["webmim_id"] == ""


def test_an_order_with_our_number_taken_by_another_order_stops_the_button(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders")] = ["timeout"]
    place(conn, mine, fake)
    stranger = fake.add_order(str(mine), "23.10.26 12:00:30")
    card(conn, webmim_id=str(stranger["id"]))
    result = place(conn, mine, fake, now="2026-10-23T12:01:00")
    assert result["state"] == "unknown" and "другом заказе панели" in result["text"] and "сверьте" in result["text"]
    assert len(fake.orders) == 1 and orders._row(conn, mine)["webmim_id"] == ""                  # no second order with our number


@pytest.mark.parametrize("created", ["23.10.26 09:00:00", "", "2026-10-23T12:00:00Z"])
def test_a_lost_order_is_found_whatever_webmims_clock_or_date_says(tmp_path, created):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders")] = ["made-then-timeout"]
    place(conn, mine, fake)
    fake.orders[0]["createDate"] = created                                             # UTC, no date, another form: never a second order
    assert place(conn, mine, fake, now="2026-10-23T12:01:00")["state"] == "done"
    assert len(fake.orders) == 1 and orders._row(conn, mine)["webmim_id"] == "1001"


def test_after_a_lost_answer_the_search_goes_before_the_dictionaries(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders")] = ["made-then-timeout"]
    place(conn, mine, fake)
    fake.payment_types = {}                                                            # a dictionary gone: the made order is still found
    assert place(conn, mine, fake, now="2026-10-23T12:01:00")["state"] == "done"


def test_a_refusal_after_a_lost_answer_keeps_the_order_looked_for(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders")] = ["timeout", "Нельзя создавать заказ задним числом."]
    assert place(conn, mine, fake)["state"] == "unknown"
    second = place(conn, mine, fake, now="2026-10-23T12:01:00")
    assert second["state"] == "unknown" and "задним числом" in second["text"]
    assert webmim_create.get_state(conn, mine)["first_at"] == NOW                     # the search still looks from the first press
    assert place(conn, mine, fake, now="2026-10-23T12:02:00")["state"] == "done" and len(fake.orders) == 1


def test_a_worker_killed_while_the_order_is_made_leaves_it_to_be_looked_for(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()

    def killed(method, url, headers, body):
        answer = fake(method, url, headers, body)
        if method == "POST" and url.endswith("/api/v2/orders"):
            raise SystemExit("the worker is stopped")
        return answer
    with pytest.raises(SystemExit):
        webmim_create.place(conn, mine, client_of(killed), "test", NOW, TODAY, "owner", sleep=lambda _s: None)
    assert webmim_create.get_state(conn, mine)["state"] == "unknown"
    assert place(conn, mine, fake, now="2026-10-23T12:01:00")["state"] == "done" and len(fake.orders) == 1


def test_a_number_typed_while_the_press_runs_stops_the_positions(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()

    def typed(method, url, headers, body):
        answer = fake(method, url, headers, body)
        if method == "POST" and url.endswith("/api/v2/orders"):
            other = store.connect(tmp_path / "t.db")
            other.execute("UPDATE order_card SET webmim_number = '777' WHERE id = ?", (mine,))
            other.commit()
            other.close()
        return answer
    result = webmim_create.place(conn, mine, client_of(typed), "test", NOW, TODAY, "owner", sleep=lambda _s: None)
    assert result["state"] == "partial" and "другой номер" in result["text"] and "№5001 (id 1001)" in result["text"]
    assert not fake.posts("/items") and orders._row(conn, mine)["webmim_number"] == "777" and orders._row(conn, mine)["webmim_id"] == ""
    with pytest.raises(orders.OrderError):
        place(conn, mine, fake, now="2026-10-23T12:05:00")


def test_cleared_webmim_fields_start_anew_and_the_old_order_is_still_found(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    place(conn, mine, fake)
    orders.update_card(conn, mine, {"webmim_id": "", "webmim_number": "", "webmim_status": ""}, NOW, "owner")     # the rollback: fields cleared
    assert webmim_create.button(conn, orders.get_card(conn, mine), "test")["label"] == "Оформить в тестовом WebMIM"
    again = place(conn, mine, fake, now="2026-10-23T12:05:00")
    assert again["state"] == "refused" and "сверьте" in again["text"] and len(fake.orders) == 1    # the order in mimtest is not deleted yet
    fake.orders.clear()                                                                # deleted in mimtest's cabinet
    assert place(conn, mine, fake, now="2026-10-23T12:06:00")["state"] == "done" and len(fake.orders) == 1


@pytest.mark.parametrize("quantity", ["две", None])
def test_an_unread_quantity_in_webmim_adds_nothing(tmp_path, quantity):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders/{id}/items")] = ["timeout"]
    place(conn, mine, fake)
    fake.items["1001"] = [{"goodsId": "111", "territoryId": TERRITORY, "stockType": 0, "quantity": quantity}]
    calls = len(fake.posts("/items"))
    result = place(conn, mine, fake, now="2026-10-23T12:05:00")
    assert result["state"] == "partial" and "не разобрано" in result["text"] and len(fake.posts("/items")) == calls


def test_a_quantity_given_as_text_counts(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders/{id}/items")] = ["timeout"]
    place(conn, mine, fake)
    fake.items["1001"] = [{"goodsId": "111", "territoryId": TERRITORY, "stockType": 0, "quantity": "3"}]
    assert place(conn, mine, fake, now="2026-10-23T12:05:00")["state"] == "done"
    assert fake.posts("/items")[-2][2]["quantity"] == 1


def test_the_secrets_never_reach_the_journal_or_the_result(tmp_path):
    conn = db(tmp_path)
    texts = []
    for failure in (["timeout"], ["500"], ["Нет остатка"]):
        mine = card(conn)
        fake = FakeWebmim()
        fake.fail[("POST", "/api/v2/orders")] = list(failure)
        texts.append(place(conn, mine, fake)["text"])
        texts += events(conn, mine) + [webmim_create.get_state(conn, mine)["result"]]
    assert texts and not any("pw-SECRET-1" in t or "TOKEN-1" in t or "api-bot" in t for t in texts)


# ---- a part made -----------------------------------------------------------------------------------------------------------------------

def test_a_refused_position_leaves_the_order_and_a_new_press_adds_only_what_is_missing(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    original = fake._answer

    def answer(method, path, query, payload):
        if method == "POST" and path.endswith("/items") and payload["goodsId"] == "222" and not getattr(answer, "done", False):
            answer.done = True
            return 422, {"field": "checkMessage", "message": "Недостаточно товара на территории 85858701785"}, {}
        return original(method, path, query, payload)
    fake._answer = answer
    first = place(conn, mine, fake)
    assert first["state"] == "partial" and "Недостаточно товара" in first["text"] and "Досоздать" in first["text"]
    assert orders._row(conn, mine)["webmim_id"] == "1001" and fake.held(1001) == {"111": 4}
    assert any("недосоздан" in t.lower() for t in events(conn, mine))
    calls = len(fake.posts("/items"))
    second = place(conn, mine, fake, now="2026-10-23T12:05:00")
    assert second["state"] == "done" and len(fake.orders) == 1
    assert [c[2]["goodsId"] for c in fake.posts("/items")[calls:]] == ["222"] and fake.held(1001) == {"111": 4, "222": 2}


def test_a_position_part_in_webmim_gets_only_the_difference(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders/{id}/items")] = ["timeout"]
    assert place(conn, mine, fake)["state"] == "partial"
    fake.items["1001"] = [{"goodsId": "111", "territoryId": TERRITORY, "stockType": 0, "quantity": 1}]       # the lost answer: one made
    assert place(conn, mine, fake, now="2026-10-23T12:05:00")["state"] == "done"
    assert fake.held(1001) == {"111": 4, "222": 2}


def test_more_in_webmim_than_in_the_order_is_left_and_said(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders/{id}/items")] = ["timeout"]
    place(conn, mine, fake)
    fake.items["1001"] = [{"goodsId": "111", "territoryId": TERRITORY, "stockType": 0, "quantity": 6}]
    result = place(conn, mine, fake, now="2026-10-23T12:05:00")
    assert fake.held(1001) == {"111": 6, "222": 2} and "больше" in result["text"]
    assert not any(c[0] in ("PUT", "DELETE") for c in fake.calls)


def test_the_search_stops_when_the_time_is_up(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    ticks = iter([0.0, 100.0])
    result = webmim_create.place(conn, mine, client_of(fake), "test", NOW, TODAY, "owner", sleep=lambda _s: None, clock=lambda: next(ticks, 100.0))
    assert result["state"] == "refused" and "время нажатия вышло" in result["text"] and not fake.posts()
    assert result["text"].startswith("WebMIM не ответил вовремя") and "недоступен" not in result["text"]


class SlowWebmim(FakeWebmim):
    """WebMIM that does not answer the list of orders in time (the socket's timeout, as urllib raises it), or answers nothing at all."""

    def __init__(self, error):
        super().__init__()
        self.error = error

    def __call__(self, method, url, headers, body):
        if method == "GET" and urllib.parse.urlsplit(url).path == "/api/v2/orders":
            raise self.error
        return super().__call__(method, url, headers, body)


@pytest.mark.parametrize("error, words", [
    (socket.timeout("timed out"), "WebMIM не ответил вовремя"),
    (urllib.error.URLError(socket.timeout("timed out")), "WebMIM не ответил вовремя"),
    (ConnectionRefusedError("refused"), "WebMIM недоступен"),
    (OSError("network is down"), "WebMIM недоступен"),
])
def test_a_slow_webmim_is_told_from_an_unreachable_one(tmp_path, error, words):
    """Review 2026-10-23: a timeout said «WebMIM недоступен»; it is «не ответил вовремя» now. Nothing is made either way."""
    conn = db(tmp_path)
    mine = card(conn)
    fake = SlowWebmim(error)
    result = place(conn, mine, fake)
    assert result["state"] == "refused" and result["text"].startswith(words) and "ничего не создано" in result["text"] and not fake.posts()


def test_the_positions_stop_when_the_time_is_up(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    result = webmim_create.place(conn, mine, client_of(fake), "test", NOW, TODAY, "owner", sleep=lambda _s: None, clock=lambda: next(ticks, 100.0))
    assert result["state"] == "partial" and orders._row(conn, mine)["webmim_id"] == "1001"


def test_finishing_a_part_made_the_next_day_is_not_stopped_by_the_date(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders/{id}/items")] = ["timeout"]
    assert place(conn, mine, fake)["state"] == "partial"
    later = place(conn, mine, fake, now="2026-10-25T10:00:00", today=date(2026, 10, 25))          # the delivery date (24th) has passed
    assert later["state"] == "done" and fake.held(1001) == {"111": 4, "222": 2}


def test_a_lost_order_is_looked_for_even_after_its_date(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders")] = ["made-then-timeout"]
    place(conn, mine, fake)
    later = place(conn, mine, fake, now="2026-10-25T10:00:00", today=date(2026, 10, 25))          # the delivery date (24th) has passed
    assert later["state"] == "done" and len(fake.orders) == 1


def test_the_test_server_makes_only_the_orders_tagged_test(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, tags="срочно")
    fake = FakeWebmim()
    with pytest.raises(orders.OrderError, match="тегом «тест»"):
        place(conn, mine, fake)
    assert fake.calls == [] and webmim_create.button(conn, orders.get_card(conn, mine), "test") is None
    assert place(conn, card(conn, tags="срочно, Тест"), fake)["state"] == "done"


def test_the_client_is_made_for_the_server_the_button_checked(tmp_path):
    conn = db(tmp_path, server="prod")
    box = SecretBox(Fernet.generate_key())
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-SECRET-1", NOW, "owner")
    conn.commit()
    assert "mimtest" in repr(webmim_match.client_for(conn, box, FakeWebmim(), "test")._base)


# ---- the checks before -----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("make, words", [
    (lambda conn: card(conn, deal_kind="stock"), "наличия точки"),
    (lambda conn: card(conn, goods=()), "позиций"),
    (lambda conn: card(conn, goods=(("x-0123456789ab", 4),)), "кода WebMIM"),
    (lambda conn: card(conn, brand="S2:cordiant"), "другого поставщика"),
    (lambda conn: card(conn, territory=""), "территори"),
    (lambda conn: card(conn, point_id=None), "точк"),
    (lambda conn: card(conn, point_id=point(conn, name="Телефон", manager_phone="+44 20 7946 0958")), "телефон не российский"),
    (lambda conn: card(conn, delivery_date=""), "дат"),
    (lambda conn: card(conn, delivery_date="2026-10-22"), "прошл"),
    (lambda conn: card(conn, point_id=point(conn, name="Без времени", default_time="")), "время"),
])
def test_a_check_before_refuses_and_nothing_goes_to_webmim(tmp_path, make, words):
    conn = db(tmp_path)
    mine = make(conn)
    fake = FakeWebmim()
    with pytest.raises(orders.OrderError) as err:
        place(conn, mine, fake)
    assert words.lower() in str(err.value).lower()
    assert fake.calls == [] and webmim_create.get_state(conn, mine) is None


def test_a_point_without_an_address_is_refused_with_the_servers_addresses(tmp_path):
    """The owner 2026-10-23 (№429): the address had to be found by a roundabout way. The refusal lists the server's addresses (a read only)."""
    conn = db(tmp_path)
    mine = card(conn, point_id=point(conn, name="Без адреса", webmim_address_id=""))
    fake = FakeWebmim()
    with pytest.raises(orders.OrderError) as err:
        place(conn, mine, fake)
    text = str(err.value)
    assert "не указан адрес доставки WebMIM" in text and "Адреса доставки на сервере «Тестовый сервер (mimtest)»: 77 — СПб, Гамма; 78 — СПб, Парнас" in text
    assert {c[0] for c in fake.calls} == {"GET"} and [c[1] for c in fake.calls][-1] == "/api/v2/addresses"
    assert webmim_create.get_state(conn, mine) is None


def test_the_addresses_not_read_do_not_hide_the_refusal(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, point_id=point(conn, name="Без адреса", webmim_address_id=""))
    fake = FakeWebmim()
    fake.fail[("GET", "/api/v2/addresses")] = ["timeout"]
    with pytest.raises(orders.OrderError) as err:
        place(conn, mine, fake)
    assert "не указан адрес доставки WebMIM" in str(err.value) and "Список адресов WebMIM получить не удалось" in str(err.value)


def test_other_refusals_do_not_ask_webmim_for_the_addresses(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, delivery_date="")
    fake = FakeWebmim()
    with pytest.raises(orders.OrderError):
        place(conn, mine, fake)
    assert fake.calls == []


def test_positions_of_two_territories_are_refused(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, goods=(("111", 4),))
    orders.add_item(conn, mine, goods_id="333", name="Шина 333", brand_key="b", season="summer", diameter=16, territory="99999999999",
                    qty=4, sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="owner")
    conn.commit()
    fake = FakeWebmim()
    with pytest.raises(orders.OrderError, match="разных территорий"):
        place(conn, mine, fake)
    assert fake.calls == []


# ---- the test server only --------------------------------------------------------------------------------------------------------------

def test_the_live_server_is_refused_without_asking_webmim(tmp_path):
    conn = db(tmp_path, server="prod")
    mine = card(conn)
    fake = FakeWebmim()
    with pytest.raises(orders.OrderError, match="только по слову владельца"):
        place(conn, mine, fake, server="prod")
    assert fake.calls == [] and webmim_create.CREATE_SERVERS == ("test",)


def test_an_order_made_on_one_server_is_not_continued_on_another(tmp_path, monkeypatch):
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    fake.fail[("POST", "/api/v2/orders/{id}/items")] = ["timeout"]
    assert place(conn, mine, fake)["state"] == "partial"
    monkeypatch.setattr(webmim_create, "CREATE_SERVERS", ("test", "prod"))
    with pytest.raises(orders.OrderError, match="другом сервере"):
        place(conn, mine, fake, server="prod")


def live_order(conn, order_id, number):
    live = {"id": order_id, "number": number, "status_code": "20", "status_text": "подтверждено", "create_date": "2026-10-23T10:00:00",
            "external_number": "", "customer_name": "Иван", "customer_phone_norm": "9111112233", "sum": 24000.0,
            "items": json.dumps([{"goods_id": "111", "name": "Шина", "qty": 4, "price": 4000, "sum": 16000},
                                 {"goods_id": "222", "name": "Шина", "qty": 2, "price": 4000, "sum": 8000}]),
            "fingerprint": "", "seen_at": NOW, "items_at": NOW}
    conn.execute("INSERT INTO webmim_order (server, %s) VALUES ('prod', %s)" % (", ".join(live), ", ".join("?" * len(live))), tuple(live.values()))


def test_a_test_order_is_kept_away_from_the_live_reconciliation(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    place(conn, mine, FakeWebmim())                                                     # test: id 1001, number 5001
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod", "login": "api-bot"}, NOW, "owner")
    live_order(conn, "1001", "5001")                                                   # the same number, phone and positions: a strong hint
    conn.commit()
    block = webmim_match.card_block(conn, orders.get_card(conn, mine))
    assert block["order"] is None and block["test_order"] and not block["not_found"] and block["hints"] == []
    rec = webmim_match.reconcile(conn, TODAY)
    assert rec["paired"] == 0 and mine not in [c["id"] for c in rec["not_found"] + rec["unpaired"]]
    assert [o["id"] for o in rec["lonely"]] == ["1001"]                                 # the live order stays free for its own order
    assert order_report.webmim_prices(conn, {mine}) == {}
    with pytest.raises(orders.OrderError):
        webmim_match.link(conn, mine, "1001", NOW, "owner")
    other = card(conn)
    webmim_match.link(conn, other, "1001", NOW, "owner")                                # the live order pairs with an order of the panel as before
    assert orders._row(conn, other)["webmim_id"] == "1001"


def test_a_test_order_whose_number_the_live_server_lacks_is_not_reported_missing(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    place(conn, mine, FakeWebmim())
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod", "login": "api-bot"}, NOW, "owner")
    live_order(conn, "2001", "6001")                                                   # a live order like ours: it would be suggested
    conn.commit()
    block = webmim_match.card_block(conn, orders.get_card(conn, mine))
    assert block["test_order"] and not block["not_found"] and block["hints"] == []
    rec = webmim_match.reconcile(conn, TODAY)
    assert mine not in [c["id"] for c in rec["not_found"] + rec["unpaired"]]


def test_a_test_order_is_shown_as_its_own_on_the_test_server(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    place(conn, mine, FakeWebmim())
    block = webmim_match.card_block(conn, orders.get_card(conn, mine))
    assert not block["test_order"]                                                      # the test server: its own order, the usual block


def test_a_number_typed_by_hand_later_is_not_a_test_order(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    place(conn, mine, FakeWebmim())
    orders.update_card(conn, mine, {"webmim_id": "", "webmim_number": "9999"}, NOW, "owner")
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod", "login": "api-bot"}, NOW, "owner")
    conn.commit()
    assert not webmim_match.card_block(conn, orders.get_card(conn, mine))["test_order"]


# ---- the page and the rights -----------------------------------------------------------------------------------------------------------

PANEL_NOW = "2026-10-07T15:00:00"          # make_app's clock: 12:00 UTC, the panel shows Moscow


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "HASH_METHOD", "pbkdf2:sha256:1000")
    box = SecretBox(Fernet.generate_key())
    fake = FakeWebmim()
    app = make_app(tmp_path, SECRET_BOX=box, WEBMIM_HTTP=fake)
    conn = store.connect(app.config["DB_PATH"])
    orders.ensure_pipeline(conn, PANEL_NOW)
    connections.set_config(conn, "webmim_orders", 0, {"server": "test", "login": "api-bot"}, PANEL_NOW, "owner")
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-SECRET-1", PANEL_NOW, "owner")
    conn.commit()
    return app, conn, fake


def person(app, conn, role, point_id=None):
    users.add_user(conn, role + "1", PASSWORD, PANEL_NOW, role=role, point_id=point_id)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = role + "1", store.get_session_epoch(conn, role + "1"), "tok"
    return client


def test_the_owner_and_the_partner_press_the_button_on_the_card(panel):
    app, conn, fake = panel
    first = card(conn, delivery_date="2026-10-08")
    second = card(conn, delivery_date="2026-10-08", point_id=point(conn, name="Парнас", webmim_address_id="78"))
    for login_, mine in (("owner", first), ("partner", second)):
        client = person(app, conn, login_) if login_ != "owner" else app.test_client()
        if login_ == "owner":
            with client.session_transaction() as session:
                session["user"], session["epoch"], session["csrf"] = "owner", 0, "tok"
                session["pw"] = shared_mark_of(client.application)
        html = client.get("/orders/%d" % mine).get_data(as_text=True)
        # the owner 2026-10-29: «Заказать в МИМ» opens the window «Подтверждение»; «Подтвердить» posts what the window showed
        assert "Заказать в МИМ</button>" in html and "Подтверждение заказа в МИМ" in html and "номер тестового заказа впишется" in html
        assert 'action="/orders/%d/webmim/create"' % mine in html and "Оформить в тестовом WebMIM" not in html
        assert "сообщения об оформлении уйдут сами" in html and "сообщения не меняются" not in html
        # review L2 (2026-10-23): every case the message does not go, no template name, nothing that reads as «a sent message can be wiped»
        for words in ("перейдёт в «Заказ оформлен»", "менеджеру точки в ВК", "номера перемещения", "нет чата Авито", "уже отправлено",
                      "пропустили вручную", "Ушедшее сообщение не удалить"):
            assert words in html
        assert "«Клиенту: заказ оформлен»" not in html and "сотрите" not in html
        seen = re.search(r'name="seen" value="([0-9a-f]+)"', html).group(1)
        assert seen == webmim_create.fingerprint(conn, orders.get_card(conn, mine))
        response = post(client, "/orders/%d/webmim/create" % mine, "tok", seen=seen)
        assert response.status_code == 302
        html = client.get("/orders/%d" % mine).get_data(as_text=True)
        assert "Оформлен в WebMIM" in html and "безнал (id 3)" in html
        assert "в тестовом WebMIM</button>" not in html
    assert len(fake.orders) == 2 and orders._row(conn, second)["webmim_number"] == "5002"


def test_the_live_server_shows_why_the_button_refuses(panel):
    app, conn, fake = panel
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod", "login": "api-bot"}, PANEL_NOW, "owner")
    conn.commit()
    mine = card(conn, delivery_date="2026-10-08")
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "owner", 0, "tok"
        session["pw"] = shared_mark_of(client.application)
    post(client, "/orders/%d/webmim/create" % mine, "tok")
    assert "только по слову владельца" in client.get("/orders/%d" % mine).get_data(as_text=True)
    assert fake.calls == []


def test_a_press_at_work_is_shown_on_the_card(panel):
    app, conn, fake = panel
    mine = card(conn, delivery_date="2026-10-08")
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, result, updated_at) "
                 "VALUES (?, 'test', 'running', ?, ?, 'partner1', 'старый итог', ?)", (mine, PANEL_NOW, PANEL_NOW, PANEL_NOW))
    conn.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "owner", 0, "tok"
        session["pw"] = shared_mark_of(client.application)
    html = client.get("/orders/%d" % mine).get_data(as_text=True)
    assert "Оформляется в WebMIM (нажал partner1" in html and "старый итог" not in html


def test_the_others_have_no_button_and_are_refused(panel):
    app, conn, fake = panel
    mine_point = point(conn)
    mine = card(conn, delivery_date="2026-10-08", point_id=mine_point)
    for role, point_id in (("chats", None), ("viewer", None), ("point", mine_point)):
        client = person(app, conn, role, point_id)
        html = client.get("/orders/%d" % mine).get_data(as_text=True)
        assert "WebMIM</button>" not in html and "webmim/create" not in html
        assert client.post("/orders/%d/webmim/create" % mine, data={"csrf": "tok"}).status_code == 403
    assert fake.calls == [] and orders._row(conn, mine)["webmim_id"] == ""
    assert users.has_right("owner", "supplier") and users.has_right("partner", "supplier")
    assert not any(users.has_right(r, "supplier") for r in ("chats", "point", "viewer"))


# ---- review round 1 (2026-10-23) -------------------------------------------------------------------------------------------------------

def test_a_card_gone_during_the_move_never_breaks_the_press(tmp_path, monkeypatch):
    conn = db(tmp_path)
    mine = card(conn)
    real = orders.get_card

    def gone(conn_, card_id):
        if (webmim_create.get_state(conn_, card_id) or {}).get("state") == "done":
            raise orders.OrderError("Такого заказа нет.")
        return real(conn_, card_id)
    monkeypatch.setattr(orders, "get_card", gone)
    result = place(conn, mine, FakeWebmim())
    assert result["ok"] and "Этап не сменился" in result["stage_error"]


def test_a_press_at_work_does_not_ask_webmim_for_the_addresses(tmp_path):
    """A refusal that is not about the address (here: the order is being made) asks WebMIM nothing, even for a point without its address."""
    conn = db(tmp_path)
    mine = card(conn)
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, webmim_id, result, updated_at) "
                 "VALUES (?, 'test', 'running', ?, ?, 'owner', '', '', ?)", (mine, NOW, NOW, NOW))
    conn.execute("UPDATE point SET webmim_address_id = '' WHERE name = 'Гамма'")
    conn.commit()
    fake = FakeWebmim()
    with pytest.raises(orders.OrderError) as err:
        place(conn, mine, fake)
    assert "уже оформляется" in str(err.value) and "Адреса доставки" not in str(err.value) and fake.calls == []


class GatewayTimeout(FakeWebmim):
    def __call__(self, method, url, headers, body):
        if method == "GET" and urllib.parse.urlsplit(url).path == "/api/v2/orders":
            return 504, {}, b"{}"
        return super().__call__(method, url, headers, body)


def test_a_gateway_timeout_is_a_slow_webmim(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    result = place(conn, mine, GatewayTimeout())
    assert result["state"] == "refused" and result["text"].startswith("WebMIM не ответил вовремя")


def test_a_stage_that_did_not_change_is_a_red_line_apart_from_the_green_result(panel):
    """Хвост З4: «Этап не сменился…» is its own red line, never glued to the green «Оформлен в WebMIM»."""
    import re
    app, conn, fake = panel
    mine = card(conn, delivery_date="2026-10-08", client_name="")
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"], session["epoch"], session["csrf"] = "owner", 0, "tok"
        session["pw"] = shared_mark_of(client.application)
    post(client, "/orders/%d/webmim/create" % mine, "tok")
    html = client.get("/orders/%d" % mine).get_data(as_text=True)
    flashes = re.findall(r'<div class="flash (flash--\w+)"[^>]*>.*?<div class="flash-text">(.*?)</div>', html, re.S)
    assert any(kind == "flash--err" and text.startswith("Этап не сменился") for kind, text in flashes)
    assert not any(kind == "flash--ok" and "Этап не сменился" in text for kind, text in flashes)


def test_why_names_the_pause_and_the_code_of_a_timeout():
    """Хвост З5: a call not made (the pause after failures) says so and until when; a gateway timeout keeps its code."""
    from tires_sync import reliability
    paused = webmim_create._why(reliability.ServiceUnavailable("webmim", 1_791_900_900.0, "HTTP 503"))
    assert "пауза после сбоев подряд до" in paused and "нет ответа сети" not in paused
    assert webmim_create._why(webmim_orders.OrdersError("gateway", status=504)) == "время ожидания ответа вышло (код 504)"
    assert webmim_create._why(TimeoutError()) == "время ожидания ответа вышло"
    assert webmim_create._why(OSError("refused")) == "нет ответа сети"


def test_no_manager_name_nor_client_phone_is_needed(tmp_path):
    """The owner 2026-10-29: the contact is «Админ» and the point's phone; the client's data does not go to WebMIM."""
    conn = db(tmp_path)
    mine = card(conn, point_id=point(conn, name="Без менеджера", manager_name=""), client_phone="", client_name="Иван")
    fake = FakeWebmim()
    assert place(conn, mine, fake)["ok"]
    sent = fake.posts("/api/v2/orders")[0][2]
    assert "customerName" not in sent and "customerPhone" not in sent and sent["deliveryPerson"] == "Админ"


class ShuffledWebmim(FakeWebmim):
    """WebMIM whose list is not by date (the live server 2026-10-28): the first page has an order older than the search's days."""

    def _answer(self, method, path, query, payload):
        if path == "/api/v2/orders" and method == "GET":
            page, per = int(query.get("page", 1)), int(query.get("per-page", 50))
            rows = list(self.orders)                                                  # the oldest first, then newer, then older again
            pages = max(1, -(-len(rows) // per))
            return 200, rows[(page - 1) * per: page * per], {"X-Pagination-Page-Count": str(pages)}
        return super()._answer(method, path, query, payload)


def test_an_order_on_a_far_page_after_an_old_one_is_found_and_not_made_twice(tmp_path):
    """The search for our number reads every page (webmim_match._every_page), not up to the first old order: an earlier press's order on
    page 3 after an old page is found; nothing is made again (the condition of the live server, the owner 2026-10-29)."""
    conn = db(tmp_path)
    mine = card(conn)
    fake = ShuffledWebmim()
    fake.add_order("new-0", created="23.10.26 10:00:00")                             # page 1: «the newest first» by its ends, an old order
    for n in range(49):                                                              # in it (a reading up to the first old order stops here)
        fake.add_order("old-%d" % n, created="01.09.26 10:00:00")
    for n in range(50):
        fake.add_order("new-%d" % (n + 1), created="23.10.26 10:00:00")              # page 2: new
    for n in range(10):
        fake.add_order("older-%d" % n, created="02.09.26 10:00:00")                  # page 3: ours among old ones
    ours = fake.add_order(str(mine), created="23.10.26 11:00:00")
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, webmim_id, result, updated_at) "
                 "VALUES (?, 'test', 'unknown', ?, ?, 'owner', '', '', ?)", (mine, NOW, NOW, NOW))
    conn.commit()
    result = place(conn, mine, fake)
    assert not fake.posts("/api/v2/orders") and result["ok"]
    assert orders._row(conn, mine)["webmim_id"] == str(ours["id"])


def test_a_new_order_with_our_number_on_a_far_page_stops_the_button(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    fake = ShuffledWebmim()
    fake.add_order("new-0", created="23.10.26 10:00:00")
    for n in range(49):
        fake.add_order("old-%d" % n, created="01.09.26 10:00:00")
    fake.add_order(str(mine), created="23.10.26 09:00:00")                           # page 2, after the old page 1
    result = place(conn, mine, fake)
    assert result["state"] == "refused" and "уже есть заказ" in result["text"] and not fake.posts("/api/v2/orders")


def test_the_window_fingerprint_must_match_the_order(tmp_path):
    """«Подтвердить» carries the fingerprint of what the window showed; the order changed meanwhile: refused, WebMIM is not asked."""
    conn = db(tmp_path)
    mine = card(conn)
    seen = webmim_create.fingerprint(conn, orders.get_card(conn, mine))
    orders.update_card(conn, mine, {"delivery_time": "после 12"}, NOW, "owner")
    fake = FakeWebmim()
    with pytest.raises(orders.OrderError) as err:
        webmim_create.place(conn, mine, client_of(fake), "test", NOW, TODAY, "owner", sleep=lambda _s: None, seen=seen)
    assert "изменились" in str(err.value) and fake.calls == [] and webmim_create.get_state(conn, mine) is None
    seen = webmim_create.fingerprint(conn, orders.get_card(conn, mine))
    result = webmim_create.place(conn, mine, client_of(fake), "test", NOW, TODAY, "owner", sleep=lambda _s: None, seen=seen)
    assert result["ok"] and fake.posts("/api/v2/orders")[0][2]["deliveryTime"] == "после 12"


@pytest.mark.parametrize("now, day, expected", [
    ("2026-10-23T16:00:00", "2026-10-23", True), ("2026-10-23T15:59:00", "2026-10-23", False), ("2026-10-23T18:00:00", "2026-10-24", False),
])
def test_late_is_today_after_sixteen(now, day, expected):
    assert webmim_create.late({"delivery_date": day}, now) is expected


def test_the_live_server_stays_closed_to_the_button():
    assert webmim_create.CREATE_SERVERS == ("test",)


# ---- review round 1 (2026-10-30, webmim-ui) ---------------------------------------------------------------------------------------------

def lost_mark(conn, mine):
    conn.execute("INSERT INTO webmim_create (order_id, server, state, first_at, started_at, author, webmim_id, result, updated_at) "
                 "VALUES (?, 'test', 'unknown', ?, ?, 'owner', '', '', ?)", (mine, NOW, NOW, NOW))
    conn.commit()


@pytest.mark.parametrize("lost", [False, True])
def test_a_list_longer_than_the_pages_read_makes_nothing(tmp_path, monkeypatch, lost):
    """Д1: more pages than MAX_PAGES — our number may be on a page not read: nothing is made (a lost answer stays «unknown»)."""
    monkeypatch.setattr(webmim_match, "MAX_PAGES", 2)
    conn = db(tmp_path)
    mine = card(conn)
    if lost:
        lost_mark(conn, mine)
    fake = FakeWebmim()
    for n in range(150):
        fake.add_order("other-%d" % n)
    result = place(conn, mine, fake)
    assert not fake.posts("/api/v2/orders") and not result["ok"]
    assert result["state"] == ("unknown" if lost else "refused") and "не все страницы" in result["text"]


class UncountedWebmim(FakeWebmim):
    """WebMIM that does not say the count of pages."""

    def __call__(self, method, url, headers, body):
        status, extra, data = super().__call__(method, url, headers, body)
        return status, {k: v for k, v in extra.items() if k != "X-Pagination-Page-Count"}, data


def test_a_list_without_its_count_read_to_the_last_full_page_makes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(webmim_match, "MAX_PAGES", 2)
    conn = db(tmp_path)
    mine = card(conn)
    fake = UncountedWebmim()
    for n in range(150):
        fake.add_order("other-%d" % n)
    result = place(conn, mine, fake)
    assert not fake.posts("/api/v2/orders") and result["state"] == "refused" and "не все страницы" in result["text"]
    fake = UncountedWebmim()                                                            # a short last page: the whole list was read
    for n in range(60):
        fake.add_order("other-%d" % n)
    assert place(conn, mine, fake)["ok"]


class RepeatingWebmim(FakeWebmim):
    """The list shifted while it was read: the same order on two pages."""

    def _answer(self, method, path, query, payload):
        status, data, extra = super()._answer(method, path, query, payload)
        if path == "/api/v2/orders" and method == "GET" and query.get("page") == "2":
            data = data + [o for o in self.orders if o["externalNumber"] == self.mine]
        return status, data, extra


def test_the_same_order_read_twice_is_one(tmp_path):
    """Д3: a row read twice (the pages shifted) is one order: the lost order is found, not «several»."""
    conn = db(tmp_path)
    mine = card(conn)
    lost_mark(conn, mine)
    fake = RepeatingWebmim()
    fake.mine = str(mine)
    fake.add_order(str(mine))
    for n in range(60):
        fake.add_order("other-%d" % n)
    result = place(conn, mine, fake)
    assert result["ok"] and not fake.posts("/api/v2/orders") and orders._row(conn, mine)["webmim_id"] == "1001"


def test_a_new_press_without_the_window_is_refused(tmp_path):
    """Д4: the page's press of a new order carries the window's fingerprint; an empty one (an old page, a hand request) is refused."""
    conn = db(tmp_path)
    mine = card(conn)
    fake = FakeWebmim()
    with pytest.raises(orders.OrderError) as err:
        webmim_create.place(conn, mine, client_of(fake), "test", NOW, TODAY, "owner", sleep=lambda _s: None, seen="")
    assert "окно подтверждения" in str(err.value) and fake.calls == []


def test_the_fingerprint_knows_the_server(tmp_path):
    conn = db(tmp_path)
    mine = card(conn)
    before = webmim_create.fingerprint(conn, orders.get_card(conn, mine))
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod", "login": "api-bot"}, NOW, "owner")
    conn.commit()
    assert webmim_create.fingerprint(conn, orders.get_card(conn, mine)) != before
