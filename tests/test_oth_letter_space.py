"""The owner 2026-11-11: the client's order cancelled by the move to «На ОТХ» (oth_storage) keeps both numbers with « О» after a space —
the WebMIM number «5367654 О» and the transfer number «26R-3568954 О»; the live numbers (without the letter) are the record's or the new
order's only. The same client back: both numbers are the order's again without the letter."""
from __future__ import annotations

from datetime import datetime

from test_oth_storage import TRANSFER, at_point, db, number, record, split, stage
from tires_sync import orders, oth_storage, search, webmim_cabinet, webmim_match

OTH_SPLIT = True


def test_the_letter_goes_after_a_space():
    assert oth_storage.with_letter("5367654") == "5367654 О"
    assert oth_storage.with_letter("26R-3568954") == "26R-3568954 О"
    assert oth_storage.with_letter("") == "" and oth_storage.with_letter(None) == ""
    assert oth_storage.with_letter("5367654 О") == "5367654 О" and oth_storage.with_letter("123О") == "123О"     # never twice


def test_both_ways_of_the_letter_are_read_as_cancelled():
    for text in ("5367654 О", "26R-3568954 О", "123О", "123 о", "123O", " 123  О "):
        assert oth_storage.refused_number(text), text
    for text in ("5367654", "26R-3568954", "", "ООО", "26R-О12"):
        assert not oth_storage.refused_number(text), text
    assert oth_storage.without_letter("26R-3568954 О") == "26R-3568954" and oth_storage.without_letter("123О") == "123"
    assert oth_storage.without_letter(oth_storage.with_letter("5367654")) == "5367654"
    assert webmim_match.own_number("5367654 О") == "" and webmim_match.own_number("5367654") == "5367654"
    assert webmim_cabinet._typed("5367654 О") == ""


def test_the_cancelled_order_keeps_both_numbers_with_the_letter(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    a, rec = orders.get_card(conn, cid), orders.get_card(conn, x)
    assert (a["webmim_number"], a["transfer_number"], a["webmim_id"]) == (number(cid) + " О", TRANSFER + " О", "")
    assert (rec["webmim_number"], rec["transfer_number"]) == (number(cid), TRANSFER)


def test_no_transfer_number_stays_empty(tmp_path):
    conn = db(tmp_path)
    cid = at_point(conn, transfer="")
    assert orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-20T11:00:00", "o", changes={"cancel_reason": "отказ"})
    assert orders.get_card(conn, cid)["transfer_number"] == ""
    assert orders.get_card(conn, record(conn, cid))["transfer_number"] == ""


def test_the_same_client_back_gets_both_numbers_without_the_letter(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    assert orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-21T09:00:00", "o")
    a = orders.get_card(conn, cid)
    assert (a["webmim_number"], a["transfer_number"]) == (number(cid), TRANSFER)
    assert (orders.get_card(conn, x)["webmim_number"], orders.get_card(conn, x)["transfer_number"]) == ("", "")


def test_the_cancelled_order_is_never_the_holder_of_the_live_numbers(tmp_path):
    conn = db(tmp_path)
    connections_prod(conn)
    cid, x = split(conn)
    conn.execute("UPDATE order_card SET transfer_number = '' WHERE id = ?", (x,))        # the record still waits for its transfer number
    conn.commit()
    assert webmim_cabinet._targets(conn, "prod") == {number(cid): x}                      # the hourly reading asks for the record only
    assert webmim_cabinet._own_number(conn, x, number(cid))


def test_the_search_finds_the_cancelled_and_the_live_transfer(tmp_path):
    conn = db(tmp_path)
    cid, x = split(conn)
    scope = search.scope_for("owner")

    def found(q):
        groups = search.run(conn, q, scope, datetime(2026, 10, 21, 12, 0))["groups"]
        return sorted(int(i["url"].rsplit("/", 1)[1]) for g in groups if g["key"] == "orders" for i in g["items"])
    assert found(TRANSFER) == [cid, x]
    assert found(number(cid)) == [cid, x]
    assert found(TRANSFER + " О") == [cid]


def connections_prod(conn):
    from tires_sync import connections
    connections.set_config(conn, "webmim_orders", 0, {"server": "prod"}, "2026-10-20T11:00:00", "owner")
