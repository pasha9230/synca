"""The invoice's number of a WebMIM order taken from WebMIM's own answers (webmim_invoice): the search, the hourly reading, the read after a press.
Every answer here is a fake one: the live WebMIM is never asked."""
from __future__ import annotations

from datetime import date

import pytest
from cryptography.fernet import Fernet

from http_fakes import ScriptedHttp
from order_fixtures import card_of_phone
from tires_sync import connections, orders, store, webmim_invoice, webmim_match, webmim_orders
from tires_sync.secretbox import SecretBox

NOW = "2026-11-07T12:00:00"
LATER = "2026-11-07T13:00:00"
TODAY = date(2026, 11, 7)
TOKEN = (200, {"accessToken": "TOKEN-123", "tokenType": "bearer"})
INVOICE = "26R-1554147"


def wm(order_id=127616450414, number=5339404, **extra):
    row = {"id": order_id, "number": number, "createDate": "07.11.26 10:00:00", "statusCode": "20", "statusDescription": "подтверждено",
           "customerName": "Пётр Пробный", "customerPhone": "+7 900 000-00-01", "createdBy": "api-bot"}
    row.update(extra)
    return row


def db(tmp_path, server="prod"):
    conn = store.connect(tmp_path / "t.db")
    orders.ensure_pipeline(conn, NOW)
    connections.set_config(conn, "webmim_orders", 0, {"server": server, "login": "api-bot"}, NOW, "owner")
    conn.commit()
    return conn


def card(conn, phone="+7 (911) 111-22-33", **fields):
    card_id = card_of_phone(conn, {"client_name": "Иван", "client_phone": phone}, NOW, "owner")
    if fields:
        orders.update_card(conn, card_id, fields, NOW, "owner")
    return card_id


def client(*replies):
    http = ScriptedHttp(TOKEN, *replies)
    return webmim_orders.OrdersClient("https://webmim.example", "api-bot", "pw-SECRET-1", http=http), http


def raws(*rows):
    return {str(r["id"]): r for r in rows}


def hourly(conn, rows, *replies, server="prod"):
    api, http = client(*replies)
    sleeps = []
    counts = webmim_invoice.hourly(conn, api, server, raws(*rows), LATER, sleeps.append)
    return counts, http, sleeps


def journal(conn, card_id):
    return orders.list_events(conn, card_id)


def by_system(conn, card_id, start=""):
    return [e for e in journal(conn, card_id) if e["author"] == "система" and e["text"].startswith(start)]


def transfer(conn, card_id):
    return orders._row(conn, card_id)["transfer_number"]


def gets(http):
    return [c for c in http.calls if "/api/v2/token" not in c[1]]


# ---- the search ------------------------------------------------------------------------------------------------------------------------

def test_the_number_is_found_in_a_field_not_described_and_never_taken_from_the_orders_own_number():
    seen = webmim_invoice.look(wm(invoiceNumber=INVOICE))
    assert seen["number"] == INVOICE and seen["where"] == "invoiceNumber"
    assert "invoiceNumber" in seen["fields"] and "number" in seen["fields"]
    seen = webmim_invoice.look(wm(docNo=INVOICE))                       # any field of the order itself
    assert seen["number"] == INVOICE and seen["where"] == "docNo"
    seen = webmim_invoice.look(wm(bill={"num": INVOICE}))               # nested under a name of an invoice
    assert seen["number"] == INVOICE and seen["where"] == "bill.num"
    assert webmim_invoice.look(wm())["number"] == ""                    # 5339404, 127616450414: digits, never an invoice


@pytest.mark.parametrize("value", ["5339404", "127616450414", "26R1554147", "26R-12", "R-1554147", "2026R-1554147", "26r-1554147",
                                   "№ 26R-1554147", "26R-1554147 от 07.11", "07.11.26", "+7 900 000-00-01"])
def test_only_the_whole_value_of_the_form_counts(value):
    assert webmim_invoice.look(wm(invoice=value))["number"] == ""


def test_a_value_deeper_under_another_name_or_two_values_are_not_taken():
    seen = webmim_invoice.look(wm(deliveryRequests=[{"id": 7, "documentNumber": "26D-0001234"}]))
    assert seen["number"] == "" and "deliveryRequests[].documentNumber" in seen["doubt"]
    seen = webmim_invoice.look(wm(invoice=INVOICE, transferDoc="26R-1554148"))
    assert seen["number"] == "" and "нескольких полях" in seen["doubt"]
    assert webmim_invoice.look(wm(invoice=INVOICE, account=INVOICE))["number"] == INVOICE     # the same value twice is one
    assert webmim_invoice.look(wm(invoice="26Р-1554147"))["number"] == "26Р-1554147"          # a Cyrillic letter


# ---- hourly ----------------------------------------------------------------------------------------------------------------------------

def test_a_number_in_the_list_goes_into_an_empty_transfer_with_a_record_by_the_system(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, webmim_id="127616450414", webmim_number="5339404")
    counts, http, _ = hourly(conn, [wm(schetNumber=INVOICE)])
    assert transfer(conn, mine) == INVOICE and counts["written"] == 1
    assert gets(http) == []                                             # the list had it: nothing asked by id
    [written] = by_system(conn, mine, "Номер перемещения из WebMIM")
    assert written["text"] == "Номер перемещения из WebMIM: 26R-1554147 (счёт заказа WebMIM №5339404 (id 127616450414))."
    [fields] = by_system(conn, mine, "Поля ответа WebMIM (список заказов")
    assert fields["level"] == "grey" and "schetNumber" in fields["text"] and "customerName" in fields["text"]
    assert INVOICE in fields["text"] and "Пётр" not in fields["text"] and "000-00-01" not in fields["text"]
    assert [e["text"] for e in by_system(conn, mine, "Номер перемещения: ")] == ["Номер перемещения: 26R-1554147"]   # the field's own record
    hourly(conn, [wm(schetNumber=INVOICE)])                             # the next hour: nothing new
    assert len(by_system(conn, mine)) == 3


def test_the_card_paired_by_its_number_alone_gets_it_too(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, webmim_number="№ 5339404")
    hourly(conn, [wm(invoice=INVOICE)])
    assert transfer(conn, mine) == INVOICE


def test_a_number_typed_by_a_person_is_kept_and_a_different_one_is_a_yellow_record_once(tmp_path):
    conn = db(tmp_path)
    same = card(conn, webmim_id="127616450414", transfer_number="26r-1554147 ")
    other = card(conn, phone="+7 (911) 111-22-34", webmim_id="127616450415", transfer_number="26R-0000001")
    rows = [wm(invoice=INVOICE), wm(127616450415, 5339405, invoice="26R-1554150")]
    counts, _, _ = hourly(conn, rows)
    assert transfer(conn, same) == "26r-1554147" and transfer(conn, other) == "26R-0000001"
    assert counts["same"] == 1 and counts["differs"] == 1 and counts["written"] == 0
    assert not [e for e in journal(conn, same) if e["level"] == "yellow"]
    [yellow] = [e for e in journal(conn, other) if e["level"] == "yellow"]
    assert "26R-0000001" in yellow["text"] and "26R-1554150" in yellow["text"] and "ничего не меняла" in yellow["text"]
    hourly(conn, rows)
    assert len([e for e in journal(conn, other) if e["level"] == "yellow"]) == 1


def test_no_number_anywhere_is_one_grey_record_of_each_answers_fields_and_nothing_else(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, webmim_id="127616450414")
    detail = (200, wm(driverInfo={"name": "Водитель", "phone": "+7 900 000-00-02"}, deliveryRequests=[{"id": 1}]))
    counts, http, sleeps = hourly(conn, [wm()], detail)
    assert transfer(conn, mine) == "" and counts["asked"] == 1 and sleeps == [webmim_match.PAUSE]
    assert [c[0] for c in http.calls] == ["GET", "GET"] and http.calls[1][1].endswith("/api/v2/orders/127616450414")
    records = by_system(conn, mine)
    assert [e["text"].split(":")[0] for e in records] == ["Поля ответа WebMIM (заказ по id, без значений)",
                                                          "Поля ответа WebMIM (список заказов, без значений)"]
    assert all(e["level"] == "grey" and "номера счёта нет" in e["text"] for e in records)
    assert "driverInfo.phone" in records[0]["text"] and "Водитель" not in records[0]["text"] and "000-00-02" not in records[0]["text"]
    hourly(conn, [wm()], detail)                                        # the next hour asks again (the invoice may come later), writes nothing
    assert len(by_system(conn, mine)) == 2


def test_a_number_given_by_id_is_written(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, webmim_id="127616450414")
    hourly(conn, [wm()], (200, wm(documents={"invoiceNo": INVOICE})))
    assert transfer(conn, mine) == INVOICE
    assert "documents.invoiceNo" in by_system(conn, mine, "Поля ответа WebMIM (заказ по id")[0]["text"]


def test_by_id_only_open_orders_without_a_transfer_and_at_most_so_many_an_hour_the_oldest_asked_first(tmp_path, monkeypatch):
    conn = db(tmp_path)
    monkeypatch.setattr(webmim_invoice, "MAX_DETAILS", 2)
    cards = [card(conn, phone="+7 (911) 111-22-%02d" % n, webmim_id=str(100 + n)) for n in range(3)]
    typed = card(conn, phone="+7 (911) 111-22-50", webmim_id="150", transfer_number="26R-0000150")
    closed = card(conn, phone="+7 (911) 111-22-51", webmim_id="151")
    done = next(s for s in orders.list_stages(conn) if s["code"] == "done")
    conn.execute("UPDATE order_card SET stage_id = ? WHERE id = ?", (done["id"], closed))
    conn.commit()
    rows = [wm(100 + n, 500 + n) for n in range(3)] + [wm(150, 550), wm(151, 551)]
    counts, http, sleeps = hourly(conn, rows, (200, wm(100, 500)), (200, wm(101, 501)))
    asked = [c[1].rsplit("/", 1)[1] for c in gets(http)]
    assert asked == ["100", "101"] and counts["asked"] == 2 and len(sleeps) == 2
    counts, http, _ = hourly(conn, rows, (200, wm(102, 502)), (200, wm(100, 500)))
    assert [c[1].rsplit("/", 1)[1] for c in gets(http)] == ["102", "100"]          # never asked first, then the one asked longest ago
    assert all(len(by_system(conn, c, "Поля ответа WebMIM (заказ по id")) == 1 for c in cards)
    assert by_system(conn, typed, "Поля ответа WebMIM (заказ по id") == [] and by_system(conn, closed, "Поля ответа WebMIM (заказ по id") == []


def test_a_failure_by_id_ends_that_part_only_and_a_gone_order_is_skipped(tmp_path):
    conn = db(tmp_path)
    first = card(conn, webmim_id="100")
    second = card(conn, phone="+7 (911) 111-22-34", webmim_id="101")
    third = card(conn, phone="+7 (911) 111-22-35", webmim_id="102")
    counts, http, _ = hourly(conn, [wm(100, 500), wm(101, 501), wm(102, 502)], (404, {"message": "нет"}), (200, wm(101, 501, invoice=INVOICE)),
                             (500, {}), (500, {}))
    assert transfer(conn, second) == INVOICE and transfer(conn, first) == "" and transfer(conn, third) == ""
    assert counts["error"] == "код 500" and webmim_invoice.line(counts).endswith("чтение по id прервано (код 500)")


def test_the_second_order_of_the_added_tyres_gets_its_transfer(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, webmim_id="100", transfer_number="26R-0000100")
    item = orders.add_item(conn, mine, goods_id="A", name="Шина A", brand_key="b", season="summer", diameter=16, territory="", qty=2,
                           sale_unit=5000, supplier_price=4000, terms=None, fee_pct=0, promo_pct=0, now=NOW, author="owner")
    item_id = item if isinstance(item, int) else conn.execute("SELECT MAX(id) FROM order_item").fetchone()[0]
    conn.execute("INSERT INTO order_mim_extra (order_id, item_id, webmim_number, transfer_number, at, author) VALUES (?,?,?,?,?,?)",
                 (mine, item_id, "5339499", "", NOW, "owner"))
    conn.commit()
    hourly(conn, [wm(100, 500, invoice="26R-0000100"), wm(127616450499, 5339499, invoice="26R-1554199")])
    assert conn.execute("SELECT transfer_number, author FROM order_mim_extra").fetchone() == ("26R-1554199", "система")
    assert transfer(conn, mine) == "26R-0000100"
    assert by_system(conn, mine, "Номер перемещения второго заказа WebMIM №5339499")
    conn.execute("UPDATE order_mim_extra SET transfer_number = '26R-7777777', author = 'owner'")
    conn.commit()
    hourly(conn, [wm(127616450499, 5339499, invoice="26R-1554199")])
    assert conn.execute("SELECT transfer_number FROM order_mim_extra").fetchone() == ("26R-7777777",)
    assert [e for e in journal(conn, mine) if e["level"] == "yellow" and "26R-7777777" in e["text"]]


def test_the_test_server_and_an_order_paired_twice_are_left_alone(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, webmim_id="127616450414")
    counts, http, _ = hourly(conn, [wm(invoice=INVOICE)], server="test")
    assert transfer(conn, mine) == "" and by_system(conn, mine) == [] and http.calls == []
    twice = card(conn, phone="+7 (911) 111-22-34", webmim_number="5339404")
    hourly(conn, [wm(invoice=INVOICE)])
    assert transfer(conn, mine) == "" and transfer(conn, twice) == ""


def test_the_hourly_run_writes_it_says_so_and_asks_webmim_with_gets_only(tmp_path):
    conn = db(tmp_path)
    box = SecretBox(Fernet.generate_key())
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod", "login": "api-bot", "auth": "login"}, NOW, "owner")
    connections.set_secret(conn, box, "webmim_orders", 0, "password", "pw-SECRET-1", NOW, "owner")
    conn.commit()
    mine = card(conn, webmim_id="127616450414")
    other = card(conn, phone="+7 (911) 111-22-34", webmim_id="100")
    http = ScriptedHttp(TOKEN, (200, [wm(accountNumber=INVOICE), wm(100, 500)], {"X-Pagination-Page-Count": "1"}),
                        (200, [], {"X-Pagination-Page-Count": "1"}), (200, [], {"X-Pagination-Page-Count": "1"}), (503, {}), (503, {}))
    status, line = webmim_match.run(conn, box, http, LATER, TODAY, sleep=lambda _s: None)
    assert status == "read" and "номер счёта: вписано 1, запросов по id 1, чтение по id прервано (код 503)" in line, line
    assert transfer(conn, mine) == INVOICE and transfer(conn, other) == ""
    assert {c[0] for c in http.calls} == {"GET"} and "pw-SECRET-1" not in line


def test_a_crash_of_the_invoice_part_never_fails_the_snapshot(tmp_path, monkeypatch):
    conn = db(tmp_path)

    def boom(*_args):
        raise RuntimeError("x")
    monkeypatch.setattr(webmim_invoice, "hourly", boom)
    api, _ = client()
    assert webmim_invoice.safe_hourly(conn, api, "prod", {}, NOW, lambda _s: None) == "; номер счёта: сбой панели"


# ---- right after a press ---------------------------------------------------------------------------------------------------------------

def test_after_a_press_one_quick_get_writes_the_number(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, webmim_id="127616450414", webmim_number="5339404")
    api, http = client((200, wm(invoice=INVOICE)))
    api.token()
    assert webmim_invoice.after_place(conn, mine, api, NOW) == "written"
    assert transfer(conn, mine) == INVOICE and len(gets(http)) == 1 and gets(http)[0][0] == "GET"
    assert by_system(conn, mine, "Поля ответа WebMIM (заказ по id")[0]["text"].endswith("номер счёта 26R-1554147 (поле invoice).")


def test_after_a_press_no_number_yet_is_a_grey_record_only_and_a_typed_one_is_kept(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, webmim_id="127616450414")
    api, _ = client((200, wm()), (200, wm(invoice=INVOICE)))
    assert webmim_invoice.after_place(conn, mine, api, NOW) == "none"
    assert [e["level"] for e in by_system(conn, mine)] == ["grey"] and transfer(conn, mine) == ""
    orders.update_card(conn, mine, {"transfer_number": "26R-0000001"}, NOW, "owner")
    assert webmim_invoice.after_place(conn, mine, api, NOW) == "differs"
    assert transfer(conn, mine) == "26R-0000001"


@pytest.mark.parametrize("reply", [(500, {}), (401, {}), (200, [1, 2])])
def test_after_a_press_a_failure_is_one_attempt_and_a_grey_line(tmp_path, reply):
    conn = db(tmp_path)
    mine = card(conn, webmim_id="127616450414")
    api, http = client(reply, reply, reply)
    api.token()
    assert webmim_invoice.after_place(conn, mine, api, NOW) == "failed"
    assert len(gets(http)) == 1                                             # never repeated
    [line] = by_system(conn, mine)
    assert line["level"] == "grey" and "почасовое" in line["text"] and "TOKEN" not in line["text"]


def test_the_quick_get_gives_the_transport_a_short_timeout_and_never_repeats_a_network_failure(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, webmim_id="127616450414")
    seen = []

    def http(method, url, headers, body, timeout=30):
        seen.append((url, timeout))
        if url.endswith("/api/v2/token"):
            import json
            return 200, {}, json.dumps({"accessToken": "T"}).encode()
        raise OSError("connection reset")
    api = webmim_orders.OrdersClient("https://webmim.example", "api-bot", "pw-SECRET-1", http=http)
    assert webmim_invoice.after_place(conn, mine, api, NOW) == "failed"
    assert seen[-1] == ("https://webmim.example/api/v2/orders/127616450414", webmim_orders.QUICK) and len(seen) == 2
    assert webmim_orders.QUICK <= 3


def test_after_a_press_without_an_id_nothing_is_asked(tmp_path):
    conn = db(tmp_path)
    mine = card(conn, webmim_number="5339404")
    api, http = client()
    assert webmim_invoice.after_place(conn, mine, api, NOW) == "no-id" and http.calls == []
