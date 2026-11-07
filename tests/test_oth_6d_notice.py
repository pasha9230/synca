"""«Нужно согласовать на возврат» (6 days 12 hours on «На ОТХ», order_oth.tell_agree_due) is not sent for a stay whose task of the return was
closed «не возвращаем» (NO_RETURN_KEY; a finding of the review of oth-return-fixes, the owner 2026-11-07): nothing to agree, the tyres are on
sale again. «пока», «ждём» are no refusal: the alert comes as before; a later stay on «На ОТХ» is told as before."""
from __future__ import annotations

from test_oth_flow2 import DUE, _held, calls
from test_oth_return_fixes import close, marked_position, whole_returning
from test_order_reserve import stage
from tires_sync import order_oth, orders


def agree_records(conn, cid):
    return [e for e in orders.list_events(conn, cid) if e["text"].startswith("Нужно согласовать")]


def test_a_stay_closed_no_return_is_not_told(tmp_path):
    conn, cid, item = marked_position(tmp_path)
    conn.execute("UPDATE order_card SET responsible = 'o' WHERE id = ?", (cid,))
    conn.commit()
    close(conn, cid, "Не возвращаем, продаём")
    told = []
    for when in (DUE, "2026-10-27T09:00:00"):
        assert order_oth.tell_agree_due(conn, when, calls(told)) == 0
    assert told == [] and agree_records(conn, cid) == []


def test_waiting_is_no_refusal_and_is_told(tmp_path):
    conn, cid, item = marked_position(tmp_path)
    close(conn, cid, "пока ждём ответа поставщика")
    assert order_oth.tell_agree_due(conn, DUE, calls([])) == 1 and len(agree_records(conn, cid)) == 1


def test_without_the_closure_it_is_told_as_before(tmp_path):
    conn, cid, _card = _held(tmp_path)
    told = []
    assert order_oth.tell_agree_due(conn, DUE, calls(told)) == 1 and len(told) == 1


def test_an_order_back_on_oth_from_return_by_no_return_is_not_told_for_that_stay(tmp_path):
    conn, cid = whole_returning(tmp_path)
    close(conn, cid, "Решили: не возвращаем")
    card = orders.get_card(conn, cid)
    assert card["stage"]["code"] == "oth"
    assert order_oth.tell_agree_due(conn, "2026-10-29T00:00:00", calls([])) == 0 and agree_records(conn, cid) == []


def test_a_later_stay_on_oth_is_told_again(tmp_path):
    conn, cid, item = marked_position(tmp_path)
    close(conn, cid, "не возвращаем")
    orders.move_card(conn, cid, stage(conn, "at_point"), "2026-10-23T10:00:00", "o")
    orders.move_card(conn, cid, stage(conn, "oth"), "2026-10-24T10:00:00", "o", {"cancel_reason": "не пришёл"})
    assert order_oth.tell_agree_due(conn, "2026-10-30T21:59:59", calls([])) == 0
    assert order_oth.tell_agree_due(conn, "2026-10-30T22:00:00", calls([])) == 1                # 6 days 12 hours of the new stay
