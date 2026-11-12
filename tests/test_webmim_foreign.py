"""A WebMIM number is unique within one customer only (the lead 2026-11-12): an order placed by hand in the cabinet under another customer
may carry the number of an order of the panel's API user. Such an order of the snapshot is foreign (webmim_match.foreign): no pair, no red,
nothing of it is written into the panel's order by the hourly reading (webmim_invoice, webmim_cabinet). One shared position: a pair, as before."""
from __future__ import annotations

from panel_helpers import login
from test_webmim_match import NOW, card, db, panel, snap, wm  # noqa: F401  (panel: the fixture)
from tires_sync import connections, orders, webmim_cabinet, webmim_invoice, webmim_match

PLACED = "2026-11-12T10:00:00"           # the card of the order placed by hand under another customer
THEIRS = "11.11.26 15:57:47"             # the API user's own order of the same number, a day before
INVOICE = "26R-1554147"


def theirs(conn, goods=(("Y", 4, 20172),), created=THEIRS, number=5355164):
    snap(conn, (dict(wm(127659878995, number, created), customerName=""), list(goods)))


def case(conn, goods=(("X", 2),), at=PLACED, **fields):
    return card(conn, at=at, goods=list(goods), **dict({"webmim_number": "5355164"}, **fields))


def test_no_position_in_common_and_older_than_the_card_is_foreign_not_a_red_pair(tmp_path):
    conn = db(tmp_path)
    theirs(conn)
    mine = case(conn)
    block = webmim_match.card_block(conn, orders.get_card(conn, mine))
    assert block["order"] is None and block["foreign"]["id"] == "127659878995"
    assert block["differences"] == [] and block["purchase"] == {} and not block["not_found"] and block["hints"] == []
    assert webmim_match.foreign_cards(conn, webmim_match.snapshot(conn, "prod")) == {mine}
    snapshot = webmim_match.snapshot(conn, "prod")
    assert webmim_match.taken(conn, snapshot) == {}                       # the order is the API user's, nobody's pair here
    page = webmim_match.reconcile(conn, webmim_match._day(PLACED))
    assert [c["id"] for c in page["not_found"]] == [mine] and [o["id"] for o in page["lonely"]] == ["127659878995"] and page["paired"] == 0


def test_the_card_says_grey_the_order_is_not_seen_and_shows_no_red(panel):
    app, conn = panel
    theirs(conn)
    mine = case(conn)
    client = app.test_client()
    login(client)
    html = client.get("/orders/%d" % mine).get_data(as_text=True)
    assert "Заказ №5355164 учётке панели не виден (вероятно, оформлен под другим заказчиком). Сверка недоступна." in html
    assert "data-webmim-foreign" in html and "Расходится" not in html and "data-webmim-diff" not in html
    assert "Autograph" not in html and "Заказ WebMIM №5355164" not in html


def test_the_hourly_invoice_writes_nothing_into_a_foreign_one_and_still_into_a_pair(tmp_path):
    conn = db(tmp_path)
    theirs(conn)
    mine = case(conn)
    raws = {"127659878995": {"id": 127659878995, "number": 5355164, "createDate": THEIRS, "statusCode": "20", "invoiceNumber": INVOICE}}
    before = conn.execute("SELECT COUNT(*) FROM order_event WHERE order_id = ?", (mine,)).fetchone()[0]
    assert webmim_invoice._targets(conn, "prod", {"127659878995": webmim_match._order(raws["127659878995"])}) == {}
    counts = webmim_invoice.hourly(conn, None, "prod", raws, NOW, lambda _s: None)
    assert counts["written"] == 0 and orders._row(conn, mine)["transfer_number"] == ""
    assert conn.execute("SELECT COUNT(*) FROM order_event WHERE order_id = ?", (mine,)).fetchone()[0] == before
    shared = case(conn, goods=(("X", 2), ("Y", 1)))                      # the control: one position in common is a pair
    conn.execute("UPDATE order_card SET webmim_number = '' WHERE id = ?", (mine,))
    conn.commit()
    assert webmim_invoice.hourly(conn, None, "prod", raws, NOW, lambda _s: None)["written"] == 1
    assert orders._row(conn, shared)["transfer_number"] == INVOICE


class Cabinet:
    def __init__(self, rows):
        self.rows, self.asked = rows, 0

    def cabinet_orders(self, page, per_page, quick=False):
        self.asked += 1
        return self.rows if page == 1 else []


def test_the_cabinet_does_not_take_the_invoice_nor_send_for_a_foreign_one(tmp_path):
    conn = db(tmp_path)
    connections.set_config(conn, "webmim_orders", connections.supplier_owner(1), {"server": "prod", "login": "api-bot"}, NOW, "owner")
    conn.commit()
    theirs(conn)
    mine = case(conn)
    assert webmim_cabinet.enabled(conn) and mine not in webmim_cabinet._targets(conn, "prod").values()
    assert not webmim_cabinet._own_number(conn, mine, "5355164")
    sent = []
    cabinet = Cabinet([{"orderNumber": 5355164, "paymentAccount": {"number": INVOICE}, "sum": 80688}])
    counts = webmim_cabinet.hourly(conn, cabinet, "prod", NOW, lambda card_id: sent.append(card_id) or [])
    assert counts["written"] == 0 and cabinet.asked == 0 and sent == [] and orders._row(conn, mine)["transfer_number"] == ""
    conn.execute("UPDATE order_card SET webmim_number = '' WHERE id = ?", (mine,))
    shared = case(conn, goods=(("Y", 4),))                                # the control: its own order is taken as before
    assert webmim_cabinet._targets(conn, "prod") == {"5355164": shared} and webmim_cabinet._own_number(conn, shared, "5355164")


def test_one_position_in_common_is_a_pair_and_differs_as_before(tmp_path):
    conn = db(tmp_path)
    theirs(conn, goods=(("Y", 4, 20172), ("X", 1, 5000)))
    mine = case(conn)
    block = webmim_match.card_block(conn, orders.get_card(conn, mine))
    assert block["foreign"] is None and block["order"]["id"] == "127659878995"
    assert {d["goods_id"] for d in block["differences"]} == {"X", "Y"}


def test_an_order_newer_than_the_card_is_a_pair_even_with_nothing_in_common(tmp_path):
    conn = db(tmp_path)
    theirs(conn, created="12.11.26 10:30:00")
    mine = case(conn)
    block = webmim_match.card_block(conn, orders.get_card(conn, mine))
    assert block["foreign"] is None and block["order"]["id"] == "127659878995" and block["differences"]


def test_a_pair_by_the_id_is_never_foreign_nor_one_whose_positions_are_not_read(tmp_path):
    conn = db(tmp_path)
    theirs(conn)
    by_id = case(conn, webmim_id="127659878995")
    block = webmim_match.card_block(conn, orders.get_card(conn, by_id))
    assert block["foreign"] is None and block["order"]["id"] == "127659878995"
    conn.execute("UPDATE webmim_order SET items = NULL")
    conn.execute("UPDATE order_card SET webmim_id = '' WHERE id = ?", (by_id,))
    conn.commit()
    block = webmim_match.card_block(conn, orders.get_card(conn, by_id))
    assert block["foreign"] is None and block["order"]["id"] == "127659878995"


def test_an_ordinary_pair_is_as_before(tmp_path):
    conn = db(tmp_path)
    theirs(conn, goods=(("X", 2, 5334),))
    mine = case(conn)
    block = webmim_match.card_block(conn, orders.get_card(conn, mine))
    assert block["foreign"] is None and block["order"]["id"] == "127659878995" and block["differences"] == []
    assert webmim_match.taken(conn, webmim_match.snapshot(conn, "prod")) == {"127659878995": mine}


def test_a_second_order_typed_with_a_foreign_number_is_not_its_pair(tmp_path):
    conn = db(tmp_path)
    theirs(conn)
    mine = case(conn, goods=(("X", 2), ("Y", 1)), webmim_number="")
    rows = {"127659878995": webmim_match._order({"id": 127659878995, "number": 5355164, "createDate": THEIRS, "statusCode": "20"})}
    items = {goods: item_id for item_id, goods in conn.execute("SELECT id, goods_id FROM order_item WHERE order_id = ?", (mine,))}
    conn.execute("INSERT INTO order_mim_extra (order_id, item_id, webmim_number, at, author) VALUES (?, ?, '5355164', ?, 'owner')",
                 (mine, items["X"], NOW))
    conn.commit()
    assert webmim_invoice._targets(conn, "prod", rows) == {}
    conn.execute("UPDATE order_mim_extra SET item_id = ?", (items["Y"],))              # the control: its position is in that order
    conn.commit()
    assert [e[1] for e in webmim_invoice._targets(conn, "prod", rows)["127659878995"]["extras"]] == [items["Y"]]
