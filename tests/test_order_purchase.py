"""The purchase of a position of an order (the owner 2026-10-21): «закупка за 1 шт» and «за N шт» in the row, not in «Итого», only with the money right;
a hand price stays; saving the WebMIM number takes the purchase from WebMIM (today: the supplier's unload; the API behind a switch, off by default)."""
from __future__ import annotations

import json
import re

from test_order_window import created, setup
from test_roles import person
from panel_helpers import post
from tires_sync import orders, webmim_orders

UNLOAD_PRICE = 5000.0                                                          # NPRICE_DELAYED of the test unload


def new_order(client, tok, **fields) -> int:
    return created(post(client, "/orders/new", tok, client_name="Иван", item_goods_id=["100", "101"], item_qty=["4", "2"], item_sale_unit=["11340", "4800"], **fields))


def items(conn, cid):
    return {i["goods_id"]: i for i in orders.list_items(conn, cid)}


def card_html(client, cid) -> str:
    page = client.get("/orders/%d" % cid).get_data(as_text=True)
    return page[page.index("<main"):] if "<main" in page else page


def audit(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT detail FROM audit WHERE action = 'order_item_purchase' ORDER BY id")]


def test_a_position_takes_its_purchase_from_the_unload_and_the_row_shows_one_and_n_pieces_but_not_the_total(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = new_order(client, tok)
    got = items(conn, cid)
    assert got["100"]["supplier_price"] == UNLOAD_PRICE and got["100"]["purchase_manual"] == 0
    body = card_html(client, cid)
    assert "Закупка за 1 шт" in body and 'name="supplier_price"' in body and 'value="5000"' in body
    assert re.search(r"за <span data-buy-qty>4</span> шт <b[^>]*>20\u00a0000", body) and re.search(r"за <span data-buy-qty>2</span> шт <b[^>]*>10\u00a0000", body)
    total = body[body.index('class="owin-total"'):]
    total = total[:total.index("</p>")]
    assert "Итого" in total and "акупк" not in total and "30 000" not in total and "20 000" not in total


def test_a_hand_purchase_is_saved_by_the_row_button_marked_journaled_and_audited(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = new_order(client, tok)
    item = items(conn, cid)["100"]
    r = post(client, "/orders/%d/items/%d/update" % (cid, item["id"]), tok, qty="4", sale_unit="11340", supplier_price="4.750,50")
    assert r.status_code == 302
    item = items(conn, cid)["100"]
    assert (item["supplier_price"], item["purchase_manual"], item["qty"]) == (4750.5, 1, 4)
    assert audit(conn)[-1] == {"order": cid, "item": item["id"], "from": UNLOAD_PRICE, "to": 4750.5, "source": "manual"}
    events = [e["text"] for e in orders.list_events(conn, cid)]
    assert any("Закупка" in t and "вручную" in t for t in events) and not any("4750" in t or "4 750" in t for t in events)   # the journal names no money
    assert "вручную" in card_html(client, cid)
    other = items(conn, cid)["101"]
    conn.execute("UPDATE order_item SET supplier_price = 5000.004 WHERE id = ?", (other["id"],))
    conn.commit()
    post(client, "/orders/%d/items/%d/update" % (cid, other["id"]), tok, qty="2", sale_unit="4800", supplier_price="5000")      # as the field showed it
    assert items(conn, cid)["101"]["purchase_manual"] == 0 and len(audit(conn)) == 1
    post(client, "/orders/%d/items/%d/update" % (cid, item["id"]), tok, qty="2", sale_unit="11340", supplier_price="4750,5")        # the same purchase: no new record
    assert len(audit(conn)) == 1 and items(conn, cid)["100"]["qty"] == 2
    r = post(client, "/orders/%d/items/%d/update" % (cid, item["id"]), tok, qty="2", sale_unit="11340", supplier_price="abc")
    assert items(conn, cid)["100"]["supplier_price"] == 4750.5                                                                    # refused, nothing changed


def test_the_webmim_number_refreshes_the_purchase_from_the_unload_but_keeps_the_hand_one(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = new_order(client, tok)
    first, second = items(conn, cid)["100"], items(conn, cid)["101"]
    post(client, "/orders/%d/items/%d/update" % (cid, first["id"]), tok, qty="4", sale_unit="11340", supplier_price="4100")
    conn.execute("UPDATE order_item SET supplier_price = 3900 WHERE id = ?", (second["id"],))                    # a purchase of an older unload
    conn.commit()
    post(client, "/orders/%d/save" % cid, tok, webmim_number="5000300")
    got = items(conn, cid)
    assert (got["100"]["supplier_price"], got["100"]["purchase_manual"]) == (4100.0, 1)                       # the hand price stays
    assert (got["101"]["supplier_price"], got["101"]["purchase_manual"]) == (UNLOAD_PRICE, 0)
    assert audit(conn)[-1] == {"order": cid, "item": second["id"], "from": 3900.0, "to": UNLOAD_PRICE, "source": "unload"}
    assert any("WebMIM" in e["text"] and "Закупка" in e["text"] for e in orders.list_events(conn, cid))
    count = len(audit(conn))
    post(client, "/orders/%d/save" % cid, tok, comment="перезвонить")                                         # the number did not change: nothing is fetched
    assert len(audit(conn)) == count


def test_a_recorded_sale_freezes_the_purchase(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = new_order(client, tok)
    item = items(conn, cid)["101"]
    conn.execute("UPDATE order_item SET supplier_price = 3900 WHERE id = ?", (item["id"],))
    conn.execute("UPDATE order_card SET sale_ids = '[1]' WHERE id = ?", (cid,))
    conn.commit()
    post(client, "/orders/%d/items/%d/update" % (cid, item["id"]), tok, qty="2", sale_unit="4800", supplier_price="100")
    post(client, "/orders/%d/save" % cid, tok, webmim_number="5000300")
    assert items(conn, cid)["101"]["supplier_price"] == 3900.0 and audit(conn) == []


def test_without_the_money_right_there_are_no_purchase_fields_values_or_saving(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = new_order(client, tok)
    item = items(conn, cid)["100"]
    for role in ("chats", "viewer"):
        other, _ = person(app, role)
        body = card_html(other, cid)
        assert "Ikon" in body or "IKON" in body
        assert "акупк" not in body and 'name="supplier_price"' not in body and "5000" not in body and "5 000" not in body and "20 000" not in body
    chats, _ = person(app, "chats", login_="chats2")
    r = chats.post("/orders/%d/items/%d/update" % (cid, item["id"]), data={"csrf": "tok", "qty": "3", "sale_unit": "11340", "supplier_price": "1"})
    assert r.status_code == 302
    item = items(conn, cid)["100"]
    assert (item["qty"], item["supplier_price"], item["purchase_manual"]) == (3, UNLOAD_PRICE, 0)          # the quantity is the manager's, the purchase is not
    partner, _ = person(app, "partner")
    assert 'name="supplier_price"' in card_html(partner, cid)


def test_the_webmim_api_is_not_called_while_its_switch_is_off(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(webmim_orders.OrdersClient, "order_items", lambda self, order_id: calls.append(order_id) or {})
    monkeypatch.setattr("tires_sync.order_offers.api_client",                               # a client is there: only the switch keeps it off
                        lambda conn, app_config: webmim_orders.OrdersClient("https://mimtest.svrauto.ru", "api", "secret", http=lambda *a: (500, {}, b"")))
    cid = new_order(client, tok)
    post(client, "/orders/%d/save" % cid, tok, webmim_id="101679153554", webmim_number="5000300")
    assert calls == [] and not app.config.get("WEBMIM_PURCHASE_API")


def test_with_the_switch_on_the_purchase_comes_from_the_webmim_order(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = new_order(client, tok)
    asked = []

    class FakeApi:
        def order_items(self, order_id):
            asked.append(order_id)
            return {"100": 4321.0}
    app.config["WEBMIM_PURCHASE_API"] = True
    monkeypatch.setattr("tires_sync.order_offers.api_client", lambda conn, app_config: FakeApi())
    post(client, "/orders/%d/save" % cid, tok, webmim_id="101679153554", webmim_number="5000300")
    got = items(conn, cid)
    assert asked == ["101679153554"] and got["100"]["supplier_price"] == 4321.0 and got["101"]["supplier_price"] == UNLOAD_PRICE
    assert audit(conn)[-1]["source"] == "webmim_api"


def test_the_client_reads_the_prices_of_the_positions_of_a_webmim_order():
    seen = []

    def http(method, url, headers, body):
        seen.append(url)
        if url.endswith("/api/v2/token"):
            return 200, {}, b'{"accessToken": "t"}'
        return 200, {}, json.dumps([{"goodsId": 100, "price": 5665, "quantity": 4}, {"goodsId": "101", "price": 4800, "quantity": 2},
                                    {"goodsId": 102, "price": None}]).encode()
    client = webmim_orders.OrdersClient("https://mimtest.svrauto.ru", "api", "secret", http=http)
    assert client.order_items("101679153554") == {"100": 5665.0, "101": 4800.0}
    assert seen[-1].startswith("https://mimtest.svrauto.ru/api/v2/orders/101679153554/items")


def test_a_failing_webmim_api_falls_back_to_the_unload_and_the_card_is_saved_and_moved(tmp_path, monkeypatch):
    """The review: the network gives OSError, not OrdersError; the card, its stage and the purchase of the unload must not depend on WebMIM."""
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = new_order(client, tok)
    second = items(conn, cid)["101"]
    conn.execute("UPDATE order_item SET supplier_price = 3900 WHERE id = ?", (second["id"],))
    conn.commit()

    def down(*args):
        raise OSError("no route to host")
    app.config["WEBMIM_PURCHASE_API"] = True
    monkeypatch.setattr("tires_sync.order_offers.api_client",
                        lambda conn, app_config: webmim_orders.OrdersClient("https://mimtest.svrauto.ru", "api", "secret", http=down))
    stage = [s for s in orders.list_stages(conn) if s["id"] != orders.get_card(conn, cid)["stage_id"]][0]
    r = post(client, "/orders/%d/save" % cid, tok, webmim_id="101679153554", webmim_number="5000300", stage_id=str(stage["id"]))
    assert r.status_code == 302
    card = orders.get_card(conn, cid)
    assert card["webmim_number"] == "5000300" and items(conn, cid)["101"]["supplier_price"] == UNLOAD_PRICE and audit(conn)[-1]["source"] == "unload"
    assert card["stage_id"] == stage["id"]                                                     # the move of the same form is done too


def test_a_corrected_number_keeps_the_purchase_and_the_webmim_id_typed_later_asks_the_api(tmp_path, monkeypatch):
    app, client, tok, conn = setup(tmp_path, monkeypatch)
    cid = new_order(client, tok)
    post(client, "/orders/%d/save" % cid, tok, webmim_number="5000300")
    second = items(conn, cid)["101"]
    conn.execute("UPDATE order_item SET supplier_price = 3900 WHERE id = ?", (second["id"],))
    conn.commit()
    post(client, "/orders/%d/save" % cid, tok, webmim_number="5000301")                       # a typo fixed days later: not today's unload
    assert items(conn, cid)["101"]["supplier_price"] == 3900.0
    asked = []

    class FakeApi:
        def order_items(self, order_id):
            asked.append(order_id)
            return {"101": 4444.0}
    app.config["WEBMIM_PURCHASE_API"] = True
    monkeypatch.setattr("tires_sync.order_offers.api_client", lambda conn, app_config: FakeApi())
    post(client, "/orders/%d/save" % cid, tok, webmim_id="101679153554")                       # the id came after the number
    assert asked == ["101679153554"] and items(conn, cid)["101"]["supplier_price"] == 4444.0
