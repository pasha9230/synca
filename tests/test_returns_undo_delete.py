"""The owner 2026-11-28 (the audit): «Вернули поставщику» only after the agreement with the supplier (an office's exception with a reason),
never for a sold order; a record of storage put there by mistake comes back on «На ОТХ»; no «Выполнено» without positions (a comment goes
to the journal); an order whose tyres came to a point is deleted by an admin only, with a warning, and kept as the important history."""
from __future__ import annotations

import json

import pytest

from panel_helpers import post
from test_order_reserve import card_at, db, stage
from test_oth_sale import panel
from tires_sync import oth_storage, orders, trash

OTH_SPLIT = True
WHEN = "2026-10-15T10:00:00"
LATER = "2026-10-21T09:00:00"
RETURN = {"return_kind": "supplier", "return_reason": "брак", "return_amount": "100"}


def code(conn, cid):
    return orders.get_card(conn, cid)["stage"]["code"]


def texts(conn, cid):
    return [e["text"] for e in orders.list_events(conn, cid)]


def audit(conn, action):
    return [json.loads(d) for (d,) in conn.execute("SELECT detail FROM audit WHERE action = ?", (action,))]


def on_oth(conn):
    cid = card_at(conn, "at_point", WHEN)
    assert orders.move_card(conn, cid, stage(conn, "oth"), LATER, "o", changes={"cancel_reason": "Клиент не пришёл"})
    return oth_storage.record_of(conn, cid)


def test_oth_goes_to_returned_only_after_the_agreement_or_as_an_exception_with_a_reason(tmp_path):
    conn = db(tmp_path)
    x = on_oth(conn)
    with pytest.raises(orders.OrderError, match="сначала согласуйте возврат"):
        orders.move_card(conn, x, stage(conn, "return"), LATER, "o", changes=RETURN)
    assert code(conn, x) == "oth"
    assert orders.move_card(conn, x, stage(conn, "return"), LATER, "o", changes=RETURN, return_exception="поставщик забрал сам")
    assert code(conn, x) == "return"
    assert any("без согласования (исключение офиса). Причина: поставщик забрал сам" in t for t in texts(conn, x))
    assert audit(conn, "return_unagreed")[0]["reason"] == "поставщик забрал сам"


def test_an_open_stage_needs_the_agreement_too_and_the_point_command_is_an_exception(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", WHEN)
    with pytest.raises(orders.OrderError, match="только после согласования"):
        orders.move_card(conn, cid, stage(conn, "return"), LATER, "o", changes=RETURN)
    assert code(conn, cid) == "at_point"


def test_a_sold_order_is_never_returned_before_the_sale_is_reversed(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", WHEN)
    assert orders.move_card(conn, cid, stage(conn, "done"), LATER, "o")
    conn.execute("UPDATE order_card SET sale_ids = '[1]' WHERE id = ?", (cid,))
    conn.commit()
    with pytest.raises(orders.OrderError, match="Сначала отмените продажу"):
        orders.move_card(conn, cid, stage(conn, "return"), LATER, "o", changes=dict(RETURN, return_kind="client"))
    assert code(conn, cid) == "done"
    conn.execute("UPDATE order_card SET sale_ids = '[]' WHERE id = ?", (cid,))
    conn.commit()
    assert orders.move_card(conn, cid, stage(conn, "return"), LATER, "o", changes=dict(RETURN, return_kind="client"))   # a client's return


def test_no_done_without_positions_and_the_comment_goes_to_the_journal(tmp_path):
    conn = db(tmp_path)
    cid = orders.create_card(conn, dict(client_name="Иван", client_phone="+7 900 000 00 01"), WHEN, "o")
    with pytest.raises(orders.OrderError, match="позици"):                 # the required data names it first, the guard after
        orders.move_card(conn, cid, stage(conn, "done"), LATER, "o")
    full = card_at(conn, "at_point", WHEN)                                  # placed with a point and a date, then emptied
    conn.execute("DELETE FROM order_item WHERE order_id = ?", (full,))
    conn.commit()
    with pytest.raises(orders.OrderError, match="позици"):
        orders.move_card(conn, full, stage(conn, "done"), LATER, "o")
    assert code(conn, full) == "at_point"
    assert code(conn, cid) != "done"
    with pytest.raises(orders.OrderError):
        orders.note_done_empty(conn, cid, LATER, "o", "  ")
    orders.note_done_empty(conn, cid, LATER, "o", "клиент забрал из наличия точки")
    assert any(t.startswith("Конфликт: «Выполнено» без позиций") for t in texts(conn, cid))
    assert audit(conn, "order_done_empty") == [{"order": cid, "comment": "клиент забрал из наличия точки"}]
    assert code(conn, cid) != "done"


def test_a_record_returned_by_mistake_comes_back_on_oth(tmp_path):
    conn = db(tmp_path)
    x = on_oth(conn)
    assert orders.move_card(conn, x, stage(conn, "return"), LATER, "o", changes=RETURN, return_exception="ошибка")
    card = orders.get_card(conn, x)
    assert oth_storage.why_not_undo_return(conn, card) is None
    assert oth_storage.undo_return(conn, x, "2026-10-22T10:00:00", "o", "перепутали записи")
    card = orders.get_card(conn, x)
    assert card["stage"]["code"] == "oth" and not card["return_kind"] and not float(card["return_amount"] or 0)
    assert conn.execute("SELECT 1 FROM oth_return_plan WHERE order_id = ?", (x,)).fetchone() is None
    assert any(t.startswith("Возврат поставщику отменён") and "перепутали записи" in t for t in texts(conn, x))
    assert audit(conn, "oth_storage_unreturn")[0]["record"] == x
    with pytest.raises(orders.OrderError, match="не на этапе"):
        oth_storage.undo_return(conn, x, "2026-10-22T11:00:00", "o")


def test_undo_return_is_for_a_record_of_storage_only(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", WHEN)
    assert orders.move_card(conn, cid, stage(conn, "return"), LATER, "o", changes=RETURN, return_exception="исключение")
    with pytest.raises(orders.OrderError, match="только у записи хранения"):
        oth_storage.undo_return(conn, cid, LATER, "o")


def test_an_order_whose_tyres_came_is_deleted_by_an_admin_only_and_kept_as_history(tmp_path):
    conn = db(tmp_path)
    cid = card_at(conn, "at_point", WHEN)
    info = trash.arrived_info(conn, cid)
    assert info == {"point": "Бета", "pieces": 4}
    assert "пропадёт из «Дохода» точки" in trash.arrived_text(info)
    with pytest.raises(orders.OrderError, match="только администратор"):
        trash.delete_order(conn, cid, LATER, "operator", role="chats")
    assert orders.get_card(conn, cid)
    record = trash.delete_order(conn, cid, LATER, "owner", role="owner")
    assert audit(conn, "order_delete_arrived")[0] == {"order": cid, "record": record, "point": "Бета", "pieces": 4}
    rows = trash.list_deleted(conn, trash.ARRIVED_KIND)
    assert [r["record_id"] for r in rows] == [cid] and rows[0]["arrived"]["pieces"] == 4
    trash.restore_order(conn, record, "2026-10-22T10:00:00", "owner")
    assert any(t.startswith("Заказ удалён после приезда шин") for t in texts(conn, cid))
    assert [r["record_id"] for r in trash.list_deleted(conn, trash.ARRIVED_KIND)] == [cid]      # the history stays once restored
    assert trash.list_deleted(conn) == []


def test_an_order_with_no_arrival_is_deleted_as_before(tmp_path):
    conn = db(tmp_path)
    cid = orders.create_card(conn, dict(client_name="Иван", client_phone="+7 900 000 00 02"), WHEN, "o")
    assert trash.arrived_info(conn, cid) is None
    trash.delete_order(conn, cid, LATER, "operator", role="chats")
    assert trash.list_deleted(conn, trash.ARRIVED_KIND) == []


def test_the_card_offers_the_exception_the_undo_and_the_warning(tmp_path):
    app, owner, tok, conn = panel(tmp_path)
    x = on_oth(conn)
    page = owner.get("/orders/%d" % x).get_data(as_text=True)
    assert "data-return-unagreed" in page and 'name="return_exception"' in page
    response = post(owner, "/orders/%d/move" % x, tok, stage_id=stage(conn, "return"), return_exception="поставщик забрал сам", **RETURN)
    assert response.status_code in (200, 302)
    assert code(conn, x) == "return"
    page = owner.get("/orders/%d" % x).get_data(as_text=True)
    assert "Отменить возврат поставщику" in page
    post(owner, "/orders/%d/return-undo" % x, tok)
    assert code(conn, x) == "oth"
    cid = card_at(conn, "at_point", WHEN)
    assert "Шины уже приезжали на точку" in owner.get("/orders/%d" % cid).get_data(as_text=True)
    assert "Шины приезжали" in owner.get("/deleted?k=arrived").get_data(as_text=True)
