"""«123О» is not «123» (the owner 2026-11-10): the order cancelled by the move to «На ОТХ» keeps its WebMIM number with the letter, and the
order of WebMIM №123 is matched with the one holding the tyres only — the record of storage, then the order that took it. The cancelled order
is never «a second order with the same number», nor «a number not found»."""
from __future__ import annotations

from datetime import date

from test_order_reserve import db, stage
from test_oth_sale import new_order, take
from test_oth_storage import LATER, MOVED, at_point, number, record, split
from tires_sync import connections, orders, webmim_cabinet, webmim_match

OTH_SPLIT = True


def snapshot(conn, cid):
    conn.execute("INSERT INTO webmim_order (server, id, number, status_code, status_text, create_date, external_number, customer_name, "
                 "customer_phone_norm, fingerprint, seen_at) VALUES ('prod', ?, ?, '1', 'создан', '2026-10-12T12:00:00', '', '', '', 'f', ?)",
                 (str(900000 + cid), number(cid), MOVED))
    conn.commit()
    return webmim_match.snapshot(conn, "prod")


def test_webmim_is_matched_with_the_holder_of_the_number_only(tmp_path):
    conn = db(tmp_path)
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod"}, MOVED, "owner")
    cid, x = split(conn)
    snap = snapshot(conn, cid)
    assert webmim_match.digits("123О") == "123" and webmim_match.own_number("123О") == "" and webmim_match.own_number("№ 123") == "123"
    assert webmim_match.own_number("123O") == "" and webmim_match.own_number("123о") == ""          # a Latin letter, a small one: typed by hand
    assert webmim_match.taken(conn, snap) == {str(900000 + cid): x} and webmim_match.shared(conn, snap) == {}
    conn.execute("UPDATE order_card SET webmim_id = '' WHERE id = ?", (x,))             # paired by the number alone: the same
    conn.commit()
    assert webmim_match.taken(conn, snap) == {str(900000 + cid): x} and webmim_match.shared(conn, snap) == {}
    seen = webmim_match.reconcile(conn, date(2026, 10, 21))
    assert seen["shared"] == [] and seen["not_found"] == [] and cid not in [c["id"] for c in seen["unpaired"]]
    block = webmim_match.card_block(conn, orders.get_card(conn, cid))
    assert block["order"] is None and not block["not_found"]
    assert webmim_match.card_block(conn, orders.get_card(conn, x))["order"]["number"] == number(cid)


def test_the_cabinet_gives_the_transfer_number_to_the_record_only(tmp_path, monkeypatch):
    conn = db(tmp_path)
    cid = at_point(conn, transfer="")
    orders.move_card(conn, cid, stage(conn, "oth"), MOVED, "o", changes={"cancel_reason": "отказ"})
    x = record(conn, cid)
    assert webmim_cabinet._targets(conn, "prod") == {number(cid): x}
    assert webmim_cabinet._own_number(conn, x, number(cid)) and webmim_cabinet._typed(number(cid) + "О") == webmim_cabinet._typed(number(cid) + " О") == ""
    b = new_order(conn, qty=4)
    take(conn, b, now=LATER)
    assert webmim_cabinet._targets(conn, "prod") == {}                                   # a borrowed number: as before
