"""Google table and records of storage (the owner 13.11: «Одна строка, этап по шинам»): a client's order cancelled by the split keeps its
one row, its «Этап» and «Реализация» follow the record its tyres went to («На ОТХ» → … → «Вернули поставщику»); the record has no row of
its own; a change of the record makes the client's row due. Made-up data only."""
from __future__ import annotations

from cryptography.fernet import Fernet

from test_order_reserve import db, stage
from test_order_sheet import turn_on
from test_oth_storage import MOVED, split
from tires_sync import order_sheet, orders
from tires_sync.secretbox import SecretBox

OTH_SPLIT = True                                  # conftest: the split into a record of storage is on
LATER = "2026-10-20T12:00:00"


def test_the_record_gets_no_row_and_the_clients_row_follows_its_tyres(tmp_path):
    conn = db(tmp_path)
    turn_on(conn, SecretBox(Fernet.generate_key()))
    cid, x = split(conn)
    order_sheet.enroll(conn, cid, MOVED)
    order_sheet.enroll(conn, x, MOVED)                                                  # a record: never in the table
    conn.commit()
    listed = {r[0] for r in conn.execute("SELECT order_id FROM sheet_order")}
    assert cid in listed and x not in listed
    rows = order_sheet.rows_of(conn, cid, MOVED)
    assert rows and all(r["Этап"] == orders.stage_name(conn, "oth") and r["Реализация"] == order_sheet.REFUSED for r in rows.values())
    def looked():                                                                       # as send_order marks it
        conn.execute("UPDATE sheet_order SET sent_at = ?, seen_event = ? WHERE order_id = ?", (MOVED, order_sheet._last_event(conn, cid), cid))
        conn.commit()
    looked()
    assert cid not in order_sheet._due(conn, LATER, False)
    orders.add_event(conn, x, LATER, "o", "field", "Комментарий: проверка")             # the record changed without changing the row
    conn.commit()
    assert cid in order_sheet._due(conn, LATER, False)
    looked()
    assert cid not in order_sheet._due(conn, LATER, False)                              # looked at once: no longer due (review 13.11)
    assert orders.move_card(conn, x, stage(conn, "return"), LATER, "o", changes={"return_amount": 1, "return_kind": "supplier", "return_reason": "невыкуп"})   # back
    conn.commit()
    assert cid in order_sheet._due(conn, LATER, False)                                  # the record changed: the client's row is due
    rows = order_sheet.rows_of(conn, cid, MOVED)
    assert all(r["Этап"] == orders.stage_name(conn, "return") and r["Реализация"] == "" for r in rows.values())


def test_a_record_already_listed_is_never_sent(tmp_path):
    conn = db(tmp_path)
    turn_on(conn, SecretBox(Fernet.generate_key()))
    cid, x = split(conn)
    conn.execute("INSERT INTO sheet_order (order_id, placed_at) VALUES (?, ?)", (x, MOVED))          # listed before 13.11
    conn.commit()
    assert x not in order_sheet._due(conn, LATER, False) and x not in order_sheet._due(conn, LATER, True)
